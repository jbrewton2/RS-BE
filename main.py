# backend/main.py
from __future__ import annotations

import os
from io import BytesIO
from typing import Optional, List

import uvicorn
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

# Core config: PdfReader, docx, FILES_DIR paths
from backend.core.config import PdfReader, docx, FILES_DIR

# Schemas & LLM review handler (legacy /analyze)
from backend.schemas import AnalyzeRequestModel, AnalyzeResponseModel
from backend.core.llm_client import call_llm_for_review

# NOTE:
# We previously experimented with backend.auth.deps and JWT-based auth.
# That pulled in 'jose' and caused ModuleNotFoundError. We've removed all
# auth dependencies from main.py for now.

# Routers
from backend.flags.router import router as flags_router
from backend.reviews.router import router as reviews_router
from backend.questionnaire.router import (
    router as questionnaire_router,
    question_bank_router,
)
from backend.knowledge.router import router as knowledge_router
from backend.llm_config.router import router as llm_config_router
from backend.pricing.router import router as pricing_router
from backend.llm_status.router import router as llm_status_router
from backend.questionnaire.sessions_router import (
    router as questionnaire_sessions_router,
)

# ---------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------

app = FastAPI(
    title="Contract Security Studio Backend",
    # No global auth dependencies for now; frontend Keycloak login is the gate.
)

origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

os.makedirs(FILES_DIR, exist_ok=True)


class ExtractResponseModel(BaseModel):
    text: str
    type: str
    pdf_url: Optional[str] = None
    pages: Optional[List[dict]] = None


# ---------------------------------------------------------------------
# /files + /extract
# ---------------------------------------------------------------------

@app.get("/files/{filename}")
async def get_file(filename: str):
    file_path = os.path.join(FILES_DIR, filename)
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path, media_type="application/pdf")


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


def extract_text_from_upload(file: UploadFile, data: bytes) -> str:
    filename = (file.filename or "").lower()
    if filename.endswith(".pdf"):
        # PDFs are handled separately so we can persist the original file
        raise HTTPException(
            status_code=400,
            detail="PDFs should be processed via the dedicated PDF path.",
        )
    if filename.endswith(".docx") or filename.endswith(".doc"):
        return _extract_text_from_docx_stream(BytesIO(data))
    try:
        return data.decode("utf-8", errors="ignore")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to decode text: {exc}")


@app.post("/extract", response_model=ExtractResponseModel)
async def extract(file: UploadFile = File(...)):
    filename = file.filename or ""
    ext = os.path.splitext(filename)[1].lower()

    try:
        contents = await file.read()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to read file: {exc}")

    if not contents:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    # DOCX
    if ext == ".docx":
        text = _extract_text_from_docx_stream(BytesIO(contents))
        return ExtractResponseModel(text=text, type="docx")

    # PDF — save original + extract text
    if ext == ".pdf":
        if PdfReader is None:
            raise HTTPException(status_code=500, detail="PDF support not installed.")
        safe_name = filename.replace(" ", "_") or "uploaded.pdf"
        pdf_path = os.path.join(FILES_DIR, safe_name)
        with open(pdf_path, "wb") as f:
            f.write(contents)
        pdf_url = f"/files/{safe_name}"
        text = _extract_text_from_pdf_stream(BytesIO(contents))
        return ExtractResponseModel(text=text, type="pdf", pdf_url=pdf_url, pages=None)

    # Treat as plain text
    text = contents.decode("utf-8", errors="ignore")
    return ExtractResponseModel(text=text, type=ext.lstrip(".") or "txt")


# ---------------------------------------------------------------------
# Legacy /analyze (direct LLM call) — kept for compatibility
# ---------------------------------------------------------------------

@app.post("/analyze", response_model=AnalyzeResponseModel)
async def analyze(req: AnalyzeRequestModel):
    """
    Contract analysis endpoint using LLM.

    NOTE: Preferred path is POST /reviews/analyze, which uses review-aware
    metadata and knowledge_doc_ids; this exists mostly for backward compat.
    """
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
        return AnalyzeResponseModel(
            summary=summary,
            risks=[],
            doc_type=None,
            deliverables=[],
        )

    summary = await call_llm_for_review(req)
    return AnalyzeResponseModel(
        summary=summary,
        risks=[],
        doc_type=None,
        deliverables=[],
    )


# ---------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------

app.include_router(flags_router)
app.include_router(reviews_router)
app.include_router(questionnaire_router)
app.include_router(question_bank_router)
app.include_router(knowledge_router)
app.include_router(llm_config_router)
app.include_router(pricing_router)
app.include_router(llm_status_router)
app.include_router(questionnaire_sessions_router)


# ---------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------

@app.get("/")
async def root():
    return {"status": "ok", "message": "CSS backend running"}


# ---------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
