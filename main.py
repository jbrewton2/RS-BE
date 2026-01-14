from __future__ import annotations

import os
from pathlib import Path
import json
from io import BytesIO
from typing import Optional, List

import uvicorn
from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

# Core config: PdfReader, docx, FILES_DIR paths
from core.config import PdfReader, docx, FILES_DIR, KNOWLEDGE_DOCS_DIR

# Schemas & LLM review handler (legacy /analyze)
from schemas import AnalyzeRequestModel, AnalyzeResponseModel
from core.llm_client import call_llm_for_review
from core.providers_root import init_providers

# Routers
from flags.router import router as flags_router
from reviews.router import router as reviews_router
from questionnaire.router import (
    router as questionnaire_router,
    question_bank_router,
)
from knowledge.router import router as knowledge_router
from llm_config.router import router as llm_config_router
from pricing.router import router as pricing_router
from llm_status.router import router as llm_status_router
from questionnaire.sessions_router import (
    router as questionnaire_sessions_router,
)

# NEW: health router (safe / unauthenticated)
from health.router import router as health_router

# ---------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------

app = FastAPI(
    title="Contract Security Studio Backend",
)

# Providers (Phase 0.5): attach provider container to app.state
app.state.providers = init_providers()

# CORS:
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
# Startup safety net: seed StorageProvider stores once (future-proof)
# ---------------------------------------------------------------------

def _pg_connect_for_startup():
    """
    Short-lived DB connection helper for startup tasks.
    Uses env vars (works for Azure sidecar Postgres).
    """
    host = os.getenv("PGHOST", "127.0.0.1")
    port = int(os.getenv("PGPORT", "5432"))
    db = os.getenv("PGDATABASE", "css")
    user = os.getenv("PGUSER", "cssadmin")
    pw = os.getenv("PGPASSWORD", "")

    # Try psycopg (new) then psycopg2 (old)
    try:
        import psycopg  # type: ignore
        return psycopg.connect(host=host, port=port, dbname=db, user=user, password=pw)
    except Exception:
        import psycopg2  # type: ignore
        return psycopg2.connect(host=host, port=port, dbname=db, user=user, password=pw)


def _ensure_pgvector_extension():
    """
    Bulletproof pgvector enablement.
    If VECTOR_STORE=pgvector, ensure CREATE EXTENSION IF NOT EXISTS vector; runs once at startup.
    Safe/idempotent. If DB not ready, fail soft.
    """
    if os.getenv("VECTOR_STORE", "").lower() != "pgvector":
        return

    try:
        conn = _pg_connect_for_startup()
        try:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            try:
                conn.commit()
            except Exception:
                pass
        finally:
            conn.close()
    except Exception:
        # Fail soft: DB may not be ready yet; vector health can still create it later.
        pass


@app.on_event("startup")
async def _ensure_storage_seeded():
    """
    Ensure provider-backed store files exist under:
      - stores/*.json
      - knowledge_docs/*.txt

    If missing, seed from legacy filesystem locations once.
    """
    storage = app.state.providers.storage

    # 1) Ensure pgvector is available (idempotent)
    _ensure_pgvector_extension()

    # 2) JSON stores: (storage_key, seed_path, empty_default)
    seed_dir = os.path.join(FILES_DIR, "seed")

    stores = [
        ("stores/reviews.json", os.path.join(seed_dir, "reviews.json"), "[]"),
        ("stores/questionnaires.json", os.path.join(seed_dir, "questionnaires.json"), "[]"),
        ("stores/question_bank.json", os.path.join(seed_dir, "question_bank.json"), "[]"),
        ("stores/knowledge_store.json", os.path.join(seed_dir, "knowledge_store.json"), "[]"),
        ("stores/flags.json", os.path.join(seed_dir, "flags.json"), "[]"),
        ("stores/flags_usage.json", os.path.join(seed_dir, "flags_usage.json"), "{}"),
        ("stores/llm_config.json", os.path.join(seed_dir, "llm_config.json"), "{}"),
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
            storage.put_object(
                key=key,
                data=data,
                content_type="application/json",
                metadata=None,
            )
        except Exception:
            pass

    # 3) Knowledge doc texts: seed from legacy KNOWLEDGE_DOCS_DIR -> storage key knowledge_docs/<filename>
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
                    storage.put_object(
                        key=storage_key,
                        data=b,
                        content_type="text/plain",
                        metadata=None,
                    )
                except Exception:
                    pass
    except Exception:
        pass


class ExtractResponseModel(BaseModel):
    text: str
    type: str
    pdf_url: Optional[str] = None
    pages: Optional[List[dict]] = None


# ---------------------------------------------------------------------
# /files + /extract
# ---------------------------------------------------------------------

@app.get("/files/{filename}")
async def get_file(filename: str, request: Request):
    storage = request.app.state.providers.storage
    try:
        data = storage.get_object(filename)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read file: {exc}")
    return Response(content=data, media_type="application/pdf")


def _extract_text_from_pdf_stream(stream: BytesIO) -> str:
    if PdfReader is None:
        raise HTTPException(status_code=500, detail="PDF support not installed.")
    try:
        reader = PdfReader(stream)
        texts: List[str] = []
        for page in reader.pages:
            try:
                page_text = page.extract_text() or ""
            except Exception:
                page_text = ""
            if page_text.strip():
                texts.append(page_text)
        return "\n".join(texts).strip() or "(No text extracted.)"
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read PDF: {exc}")


def _extract_text_from_docx_stream(stream: BytesIO) -> str:
    if docx is None:
        raise HTTPException(status_code=500, detail="DOCX support not installed.")
    try:
        document = docx.Document(stream)
        paras = [p.text for p in document.paragraphs]
        return "\n".join(paras).strip() or "(No text extracted.)"
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read DOCX: {exc}")


# Provide BOTH routes so the frontend can call /api/extract
@app.post("/extract", response_model=ExtractResponseModel)
@app.post("/api/extract", response_model=ExtractResponseModel)
async def extract(request: Request, file: UploadFile = File(...)):
    """
    NOTE:
    - Request must be a real dependency param (not Optional default None),
      otherwise FastAPI will try to treat it as a Pydantic field and crash.
    """
    filename = file.filename or ""
    ext = os.path.splitext(filename)[1].lower()

    try:
        contents = await file.read()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to read file: {exc}")

    if not contents:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    if ext == ".docx":
        text = _extract_text_from_docx_stream(BytesIO(contents))
        return ExtractResponseModel(text=text, type="docx")

    if ext == ".pdf":
        if PdfReader is None:
            raise HTTPException(status_code=500, detail="PDF support not installed.")
        safe_name = filename.replace(" ", "_") or "uploaded.pdf"

        storage = request.app.state.providers.storage
        storage.put_object(
            key=safe_name,
            data=contents,
            content_type="application/pdf",
            metadata=None,
        )

        pdf_url = f"/files/{safe_name}"
        text = _extract_text_from_pdf_stream(BytesIO(contents))
        return ExtractResponseModel(text=text, type="pdf", pdf_url=pdf_url, pages=None)

    text = contents.decode("utf-8", errors="ignore")
    return ExtractResponseModel(text=text, type=ext.lstrip(".") or "txt")


# ---------------------------------------------------------------------
# Legacy /analyze (direct LLM call) — kept for compatibility
# ---------------------------------------------------------------------

# Provide BOTH routes so the frontend can call /api/analyze
@app.post("/analyze", response_model=AnalyzeResponseModel)
@app.post("/api/analyze", response_model=AnalyzeResponseModel)
async def analyze(req: AnalyzeRequestModel):
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


# ---------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------
# Health router stays mounted as-is (it already defines /health AND /api/* db endpoints)
app.include_router(health_router)

# Everything else must be reachable under /api/* for the frontend + AFD routing.
app.include_router(flags_router, prefix="/api")
app.include_router(reviews_router, prefix="/api")
app.include_router(questionnaire_router, prefix="/api")
app.include_router(question_bank_router, prefix="/api")
app.include_router(knowledge_router, prefix="/api")
app.include_router(llm_config_router, prefix="/api")
app.include_router(pricing_router, prefix="/api")
app.include_router(llm_status_router, prefix="/api")
app.include_router(questionnaire_sessions_router, prefix="/api")


@app.get("/")
async def root():
    return {"status": "ok", "message": "CSS backend running"}


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
