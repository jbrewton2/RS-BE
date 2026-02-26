from __future__ import annotations

import hashlib
import json
import time
from typing import Optional, Tuple

from fastapi import HTTPException, Request, UploadFile

from core.deps import StorageDep


def _pdf_key_for_doc(doc_id: str) -> str:
    return f"review_pdfs/{doc_id}.pdf"


def _extract_keys_for_doc(doc_id: str) -> Tuple[str, str]:
    return (f"extract/{doc_id}/raw_text.txt", f"extract/{doc_id}/extract.json")


async def extract_and_persist(storage: StorageDep, request: Request, file: UploadFile, *, doc_id: str) -> dict:
    """
    Extract text from an uploaded file and persist:
      - review_pdfs/<doc_id>.pdf
      - extract/<doc_id>/raw_text.txt
      - extract/<doc_id>/extract.json

    This function is intentionally "router-safe" (no router imports) to avoid circular imports.
    It should contain the same core logic previously implemented in main._extract_impl.
    """
    if file is None:
        raise HTTPException(status_code=400, detail="Missing file")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    filename = (file.filename or "").strip() or "upload"
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""

    # --- Convert to PDF bytes (if needed) ---
    # NOTE: We do NOT implement DOCX->PDF conversion here unless the repo already has a helper.
    # If you already have conversion in main.py, move that logic here (or call an existing util).
    pdf_bytes: Optional[bytes] = None

    if ext == "pdf":
        pdf_bytes = bytes(data)
    else:
        # For now, store the original bytes as-is NOT OK for viewer. We will fail fast until conversion is wired.
        # This makes the bug obvious instead of silently breaking doc viewer.
        raise HTTPException(status_code=415, detail=f"Unsupported file type for review doc upload: {ext}. Upload PDF for now.")

    # --- Extract text from PDF bytes ---
    try:
        # reuse existing deterministic PDF text extraction
        from rag.ingestion_engine import _extract_text_from_pdf_bytes
        text = _extract_text_from_pdf_bytes(pdf_bytes) or ""
    except Exception:
        text = ""

    # --- Persist ---
    pdf_key = _pdf_key_for_doc(doc_id)
    raw_key, extract_json_key = _extract_keys_for_doc(doc_id)

    # pdf
    storage.put_object(
        key=pdf_key,
        data=pdf_bytes,
        content_type="application/pdf",
        metadata=None,
    )

    # raw text
    raw_text_bytes = text.encode("utf-8", errors="ignore")
    storage.put_object(
        key=raw_key,
        data=raw_text_bytes,
        content_type="text/plain; charset=utf-8",
        metadata=None,
    )

    payload = {
        "doc_id": doc_id,
        "pdf_key": pdf_key,
        "pdf_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
        "extract_text_sha256": hashlib.sha256(raw_text_bytes).hexdigest(),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "filename": filename,
    }
    storage.put_object(
        key=extract_json_key,
        data=json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8", errors="ignore"),
        content_type="application/json",
        metadata=None,
    )

    return {
        "doc_id": doc_id,
        "filename": filename,
        "pdf_url": f"/files/{pdf_key}",
        "text": text,
        "type": "pdf",
        "pages": None,
    }