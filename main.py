from __future__ import annotations

from contextlib import asynccontextmanager
from io import BytesIO
from typing import Optional, List

import os
import re
import tempfile
import subprocess
import uuid
import json

import uvicorn
from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, HTMLResponse
from fastapi.openapi.docs import get_swagger_ui_html, get_redoc_html
from pydantic import BaseModel

from core.settings import get_settings

# Core config: PdfReader, docx, FILES_DIR paths
from core.config import PdfReader, docx, FILES_DIR, KNOWLEDGE_DOCS_DIR

# Schemas & LLM review handler (legacy /analyze)
from schemas import AnalyzeRequestModel, AnalyzeResponseModel
from core.llm_client import call_llm_for_review
from core.providers import init_providers
from core.dynamo_meta import DynamoMeta, sha256_bytes, sha256_text, _now_iso  # noqa: F401

# Routers
from flags.router import router as flags_router
from reviews.router import router as reviews_router
from questionnaire.router import (
    router as questionnaire_router,
    question_bank_router,
)
from knowledge.router import router as knowledge_router
from pricing.router import router as pricing_router
from questionnaire.sessions_router import (
    router as questionnaire_sessions_router,
)
from rag.router import router as rag_router

# Health router (safe / unauthenticated)
from health.router import router as health_router


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _guess_media_type(key: str) -> str:
    """
    Best-effort Content-Type based on key extension.
    """
    k = (key or "").lower()
    if k.endswith(".pdf"):
        return "application/pdf"
    if k.endswith(".docx"):
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if k.endswith(".txt"):
        return "text/plain; charset=utf-8"
    if k.endswith(".json"):
        return "application/json; charset=utf-8"
    return "application/octet-stream"


def _safe_filename(name: str) -> str:
    """
    Sanitize filename for logging/metadata purposes only.
    We do NOT use filename as a storage key (avoids collisions).
    """
    name = (name or "upload").strip()
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r"[^A-Za-z0-9\.\-_# ]+", "_", name).strip("_")
    return name[:180] or "upload"


def _extract_text_from_pdf_stream(stream: BytesIO) -> tuple[str, list[dict]]:
    """
    Extract text from a PDF stream and also return deterministic page->char span mapping.

    pages = [{ "pageNumber": 1, "charStart": 0, "charEnd": 1234 }, ...]
    charStart/charEnd are offsets into the returned concatenated text.
    """
    if PdfReader is None:
        raise HTTPException(status_code=500, detail="PDF support not installed.")

    reader = PdfReader(stream)

    chunks: list[str] = []
    pages: list[dict] = []

    cursor = 0
    page_num = 0

    for page in reader.pages:
        page_num += 1
        try:
            txt = page.extract_text() or ""
        except Exception:
            txt = ""

        # Normalize
        txt = (txt or "").strip()
        if not txt:
            # still record a span (zero-width) so page count is consistent
            pages.append({"pageNumber": page_num, "charStart": cursor, "charEnd": cursor})
            continue

        # Add separator between pages to keep offsets stable and readable
        if chunks:
            chunks.append("\n\n")
            cursor += 2

        start = cursor
        chunks.append(txt)
        cursor += len(txt)
        end = cursor

        pages.append({"pageNumber": page_num, "charStart": start, "charEnd": end})

    text = "".join(chunks).strip()
    return text, pages
def _extract_text_from_docx_stream(stream: BytesIO) -> str:
    if docx is None:
        raise HTTPException(status_code=500, detail="DOCX support not installed.")
    try:
        document = docx.Document(stream)
        paras = [p.text for p in document.paragraphs]
        return "\n".join(paras).strip() or "(No text extracted.)"
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read DOCX: {exc}")


def _convert_docx_bytes_to_pdf_bytes(
    docx_bytes: bytes,
    *,
    timeout_seconds: int = 120,
    work_root: str = "/tmp/css-doc-conversion",
) -> Optional[bytes]:
    """
    Convert DOCX bytes -> PDF bytes using LibreOffice (soffice).
    Returns None if conversion fails.
    SAFE + NON-BLOCKING: conversion failure must NOT break extraction.
    """
    soffice = os.environ.get("SOFFICE_PATH", "soffice")

    # fast-fail if soffice not present
    try:
        subprocess.run([soffice, "--version"], capture_output=True, text=True, check=False)
    except Exception:
        return None

    # Ensure work root exists (fall back to system temp if not)
    try:
        os.makedirs(work_root, exist_ok=True)
        td_parent = work_root
    except Exception:
        td_parent = None

    with tempfile.TemporaryDirectory(prefix="css-docx2pdf-", dir=td_parent) as td:
        in_path = os.path.join(td, "input.docx")
        out_dir = os.path.join(td, "out")
        os.makedirs(out_dir, exist_ok=True)

        with open(in_path, "wb") as f:
            f.write(docx_bytes)

        cmd = [
            soffice,
            "--headless",
            "--nologo",
            "--nofirststartwizard",
            "--norestore",
            "--nolockcheck",
            "--nodefault",
            "--convert-to",
            "pdf",
            "--outdir",
            out_dir,
            in_path,
        ]

        # Harden LO runtime so it writes profiles/temp inside the temp directory
        env = os.environ.copy()
        env["HOME"] = td
        env["TMPDIR"] = td

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds, check=False, env=env)
        except subprocess.TimeoutExpired:
            return None
        except Exception:
            return None

        if proc.returncode != 0:
            return None

        # LibreOffice writes output with same basename
        pdf_path = os.path.join(out_dir, "input.pdf")
        if not os.path.exists(pdf_path):
            candidates = [p for p in os.listdir(out_dir) if p.lower().endswith(".pdf")]
            if not candidates:
                return None
            pdf_path = os.path.join(out_dir, candidates[0])

        try:
            with open(pdf_path, "rb") as f:
                return f.read()
        except Exception:
            return None


def _storage_key(key: str) -> str:
    """
    Normalize object keys for prefixed storage (S3_PREFIX=stores, etc).
    If S3_PREFIX is empty, return key unchanged.
    Ensures no double slashes.
    """
    k = (key or "").lstrip("/")
    prefix = (os.environ.get("S3_PREFIX") or os.environ.get("DOCS_PREFIX") or "").strip().strip("/")
    if not prefix:
        return k
    return f"{prefix}/{k}".replace("//", "/")


def _pdf_key_for_doc_id(doc_id: str) -> str:
    # Canonical: do not use filename as key (avoids collision)
    return _storage_key(f"review_pdfs/{doc_id}.pdf")


def _extract_artifact_keys(doc_id: str) -> tuple[str, str]:
    # Canonical extract artifacts path (RAG ingest reads these)
    return (
        _storage_key(f"extract/{doc_id}/raw_text.txt"),
        _storage_key(f"extract/{doc_id}/extract.json"),
    )# ---------------------------------------------------------------------
# Lifespan (seed storage)
# ---------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Ensure provider-backed store files exist under:
      - stores/*.json
      - knowledge_docs/*.txt
    Seed once from FILES_DIR/seed + KNOWLEDGE_DOCS_DIR.

    IMPORTANT:
    - This does NOT define the storage backend.
    - Storage is selected ONLY by core.settings + init_providers().
    """
    storage = app.state.providers.storage
    seed_dir = os.path.join(FILES_DIR, "seed")

    stores = [
        ("stores/reviews.json", os.path.join(seed_dir, "reviews.json"), "[]"),
        ("stores/questionnaires.json", os.path.join(seed_dir, "questionnaires.json"), "[]"),
        ("stores/question_bank.json", os.path.join(seed_dir, "question_bank.json"), "[]"),
        ("stores/knowledge_store.json", os.path.join(seed_dir, "knowledge_store.json"), "[]"),
        ("stores/flags.json", os.path.join(seed_dir, "flags.json"), "[]"),
        ("stores/flags_usage.json", os.path.join(seed_dir, "flags_usage.json"), "{}"),
        ("stores/llm_pricing.json", os.path.join(seed_dir, "llm_pricing.json"), "{}"),
        ("stores/llm_stats.json", os.path.join(seed_dir, "llm_stats.json"), "[]"),
    ]

    for key, seed_path, empty_default in stores:
        try:
            storage.head_object(key)
            continue
        except Exception:
            pass

        data = None
        try:
            if os.path.exists(seed_path):
                with open(seed_path, "rb") as f:
                    data = f.read()
        except Exception:
            data = None

        if not data:
            data = empty_default.encode("utf-8")

        try:
            storage.put_object(key=key, data=data, content_type="application/json", metadata=None)
        except Exception:
            pass

    # Knowledge docs (.txt) seed
    try:
        legacy_docs_dir = KNOWLEDGE_DOCS_DIR
        if os.path.isdir(legacy_docs_dir):
            for name in os.listdir(legacy_docs_dir):
                if not name.endswith(".txt"):
                    continue
                legacy_file = os.path.join(legacy_docs_dir, name)
                storage_key = f"knowledge_docs/{name}"

                try:
                    storage.head_object(storage_key)
                    continue
                except Exception:
                    pass

                try:
                    with open(legacy_file, "rb") as f:
                        b = f.read()
                    storage.put_object(key=storage_key, data=b, content_type="text/plain", metadata=None)
                except Exception:
                    pass
    except Exception:
        pass

    yield


# ---------------------------------------------------------------------
# App
# ---------------------------------------------------------------------

app = FastAPI(
    title="Contract Security Studio Backend",
    lifespan=lifespan,
)

# Providers: attach provider container to app.state.
# SINGLE SOURCE OF TRUTH: core.settings.get_settings() drives init_providers().
app.state.providers = init_providers(app)

# CORS (dev only; in prod we use same-origin proxy)
origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8080",
    "http://127.0.0.1:8080",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------

class ExtractByKeyRequest(BaseModel):
    review_id: str
    pdf_key: str


class ExtractResponseModel(BaseModel):
    text: str
    type: str
    pdf_url: Optional[str] = None
    pages: Optional[List[dict]] = None
    doc_id: Optional[str] = None
    filename: Optional[str] = None


# ---------------------------------------------------------------------
# Files + Extract
# ---------------------------------------------------------------------

@app.get("/files/{key:path}")
async def get_file(key: str, request: Request):
    """
    Serve stored files from the StorageProvider.

    IMPORTANT:
    - This reads from the active StorageProvider (S3 in GovCloud).
    - No local filesystem reads occur here.
    """
    storage = request.app.state.providers.storage
    if not key:
        raise HTTPException(status_code=400, detail="Missing key")

    try:
        data = storage.get_object(key)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read file: {exc}")

    if not data:
        raise HTTPException(status_code=404, detail="File not found")

    return Response(content=data, media_type=_guess_media_type(key))


async def _write_extract_artifacts(
    *,
    storage,
    doc_id: str,
    review_id: Optional[str],
    pdf_key: Optional[str],
    pdf_bytes: Optional[bytes],
    extracted_text: str,
) -> tuple[str, str, str, str]:
    """
    Writes:
      - extract/<doc_id>/raw_text.txt
      - extract/<doc_id>/extract.json

    Returns:
      (extract_text_key, extract_text_sha256, extract_json_key, extract_json_sha256)
    """
    extract_text_key, extract_json_key = _extract_artifact_keys(doc_id)

    raw_text_bytes = (extracted_text or "").encode("utf-8", errors="ignore")
    payload = {
        "doc_id": doc_id,
        "review_id": (review_id or "").strip() or None,
        "pdf_key": (pdf_key or "").strip() or None,
        "pdf_sha256": sha256_bytes(pdf_bytes) if pdf_bytes else None,
        "extract_text_sha256": sha256_bytes(raw_text_bytes),
        "created_at": _now_iso(),
    }
    extract_json_bytes = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8", errors="ignore")

    storage.put_object(
        key=extract_text_key,
        data=raw_text_bytes,
        content_type="text/plain; charset=utf-8",
        metadata=None,
    )
    storage.put_object(
        key=extract_json_key,
        data=extract_json_bytes,
        content_type="application/json",
        metadata=None,
    )

    return (
        extract_text_key,
        sha256_bytes(raw_text_bytes),
        extract_json_key,
        sha256_bytes(extract_json_bytes),
    )


async def _extract_impl(request: Request, file: UploadFile) -> ExtractResponseModel:
    filename_raw = file.filename or "upload"
    filename = _safe_filename(filename_raw)
    ext = os.path.splitext(filename_raw)[1].lower()

    try:
        contents = await file.read()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to read file: {exc}")

    if not contents:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    storage = request.app.state.providers.storage
    doc_id = str(uuid.uuid4())

    # DOCX: extract text + convert to PDF (non-blocking)
    if ext == ".docx":
        text = _extract_text_from_docx_stream(BytesIO(contents))

        pdf_bytes = _convert_docx_bytes_to_pdf_bytes(contents)
        pdf_url = None
        pdf_key = None

        if pdf_bytes:
            pdf_key = _pdf_key_for_doc_id(doc_id)
            try:
                storage.put_object(key=pdf_key, data=pdf_bytes, content_type="application/pdf", metadata=None)
                pdf_url = f"/files/{pdf_key}"
            except Exception:
                pdf_url = None

        # Always write extract artifacts for RAG (even if pdf conversion failed)
        try:
            await _write_extract_artifacts(
                storage=storage,
                doc_id=doc_id,
                review_id=None,
                pdf_key=pdf_key,
                pdf_bytes=pdf_bytes,
                extracted_text=text,
            )
        except Exception:
            # non-blocking: extraction still returns text
            pass

        return ExtractResponseModel(
            text=text,
            type="docx",
            pdf_url=pdf_url,
            pages=None,
            doc_id=doc_id,
            filename=filename,
        )

    # PDF: store PDF + extract text
    if ext == ".pdf":
        pdf_key = _pdf_key_for_doc_id(doc_id)
        try:
            storage.put_object(key=pdf_key, data=contents, content_type="application/pdf", metadata=None)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to store PDF: {exc}")

        pdf_url = f"/files/{pdf_key}"
        text = _extract_text_from_pdf_stream(BytesIO(contents))

        # Always write extract artifacts for RAG
        try:
            await _write_extract_artifacts(
                storage=storage,
                doc_id=doc_id,
                review_id=None,
                pdf_key=pdf_key,
                pdf_bytes=contents,
                extracted_text=text,
            )
        except Exception:
            pass

        return ExtractResponseModel(
            text=text,
            type="pdf",
            pdf_url=pdf_url,
            pages=pages,
            doc_id=doc_id,
            filename=filename,
        )

    # TXT or fallback
    try:
        text = contents.decode("utf-8", errors="replace").strip()
    except Exception:
        text = ""

    try:
        await _write_extract_artifacts(
            storage=storage,
            doc_id=doc_id,
            review_id=None,
            pdf_key=None,
            pdf_bytes=None,
            extracted_text=text,
        )
    except Exception:
        pass

    return ExtractResponseModel(
        text=text,
        type=ext.lstrip(".") or "txt",
        pdf_url=None,
        pages=None,
        doc_id=doc_id,
        filename=filename,
    )


@app.post("/extract", response_model=ExtractResponseModel)
async def extract(request: Request, file: UploadFile = File(...)):
    return await _extract_impl(request=request, file=file)


@app.post("/api/extract", response_model=ExtractResponseModel, include_in_schema=True)
async def api_extract(request: Request, file: UploadFile = File(...)):
    return await _extract_impl(request=request, file=file)


@app.post("/api/extract-by-key", response_model=ExtractResponseModel, include_in_schema=True)
async def api_extract_by_key(req: ExtractByKeyRequest, request: Request):
    """
    Extract text from an existing PDF already stored in StorageProvider (S3).
    Persists:
      - extracted text artifact to S3 at extract/<doc_id>/raw_text.txt
      - extract.json to S3 at extract/<doc_id>/extract.json
      - pointers + sha256 to Dynamo (REVIEW#{review_id}/META)
    """
    storage = request.app.state.providers.storage

    review_id = (req.review_id or "").strip()
    pdf_key = (req.pdf_key or "").strip()
    if not review_id or not pdf_key:
        raise HTTPException(status_code=400, detail="review_id and pdf_key are required")

    try:
        pdf_bytes = storage.get_object(pdf_key)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="PDF key not found")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read PDF from storage: {exc}")

    if not pdf_bytes:
        raise HTTPException(status_code=404, detail="PDF key not found")

    # deterministic doc_id for extract-by-key: doc_id == the review_id (stable pointer)
    doc_id = review_id
    pdf_url = f"/files/{pdf_key}"

    text = _extract_text_from_pdf_stream(BytesIO(pdf_bytes))

    try:
        extract_text_key, extract_text_sha, extract_json_key, extract_json_sha = await _write_extract_artifacts(
            storage=storage,
            doc_id=doc_id,
            review_id=review_id,
            pdf_key=pdf_key,
            pdf_bytes=pdf_bytes,
            extracted_text=text,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to store extract artifacts: {exc}")

    # Write pointers + hashes to Dynamo
    meta = DynamoMeta()
    meta.upsert_review_meta(
        review_id,
        pdf_key=pdf_key,
        pdf_sha256=sha256_bytes(pdf_bytes),
        pdf_size=len(pdf_bytes),
        extract_text_key=extract_text_key,
        extract_text_sha256=extract_text_sha,
        extract_json_key=extract_json_key,
        extract_json_sha256=extract_json_sha,
    )

    return ExtractResponseModel(text=text, type="pdf", pdf_url=pdf_url, pages=pages, doc_id=doc_id, filename=None)


# ---------------------------------------------------------------------
# Legacy /analyze (compat)
# ---------------------------------------------------------------------

async def _analyze_impl(req: AnalyzeRequestModel) -> AnalyzeResponseModel:
    text = (req.text or "").strip()
    if not text or "no text extracted" in text.lower():
        summary = (
            "OBJECTIVE\n"
            "- Insufficient machine-readable text.\n\n"
            "SCOPE\n"
            "- Unable to determine scope.\n\n"
            "KEY REQUIREMENTS\n"
            "- None detected.\n\n"
            "KEY RISKS\n"
            "- Manual review required.\n\n"
            "GAPS AND AMBIGUITIES\n"
            "- Not enough text available.\n\n"
            "RECOMMENDED NEXT STEPS\n"
            "- Obtain a native PDF or text-based source.\n"
        )
        return AnalyzeResponseModel(summary=summary, risks=[], doc_type=None, deliverables=[])

    summary = await call_llm_for_review(req)
    return AnalyzeResponseModel(summary=summary, risks=[], doc_type=None, deliverables=[])


@app.post("/analyze", response_model=AnalyzeResponseModel)
async def analyze(req: AnalyzeRequestModel):
    return await _analyze_impl(req)


@app.post("/api/analyze", response_model=AnalyzeResponseModel, include_in_schema=True)
async def api_analyze(req: AnalyzeRequestModel):
    return await _analyze_impl(req)


# ---------------------------------------------------------------------
# Health + OpenAPI helpers
# ---------------------------------------------------------------------

@app.get("/api/health", include_in_schema=True)
def api_health():
    return {"ok": True}


@app.get("/api/openapi.json", include_in_schema=False)
def api_openapi():
    return app.openapi()


@app.get("/api/docs", include_in_schema=False)
def api_docs() -> HTMLResponse:
    return get_swagger_ui_html(openapi_url="/api/openapi.json", title="CSS Backend API Docs")


@app.get("/api/redoc", include_in_schema=False)
def api_redoc() -> HTMLResponse:
    return get_redoc_html(openapi_url="/api/openapi.json", title="CSS Backend API Docs (ReDoc)")


# ---------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------

# Health router at root (already defines /health and /api/db/*)
app.include_router(health_router)

# Sessions at root + /api (backwards compat)
app.include_router(questionnaire_sessions_router)
app.include_router(questionnaire_sessions_router, prefix="/api")

# Functional routers under /api
app.include_router(flags_router, prefix="/api")
app.include_router(reviews_router, prefix="/api")
app.include_router(questionnaire_router, prefix="/api")
app.include_router(question_bank_router, prefix="/api")
app.include_router(knowledge_router, prefix="/api")
app.include_router(pricing_router, prefix="/api")
app.include_router(rag_router, prefix="/api")


@app.get("/")
async def root():
    return {"status": "ok", "message": "CSS backend running"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)


