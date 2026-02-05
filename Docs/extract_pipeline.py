# backend/documents/extract_pipeline.py
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Optional, Tuple

from backend.documents.convert import convert_docx_bytes_to_pdf_bytes


def _safe_filename(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return "upload"
    # Remove path separators and odd chars
    name = name.replace("\\", "_").replace("/", "_")
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name)
    return name[:180]


def _guess_type(filename: str, content_type: Optional[str]) -> str:
    fn = (filename or "").lower()
    ct = (content_type or "").lower()
    if fn.endswith(".pdf") or "pdf" in ct:
        return "pdf"
    if fn.endswith(".docx") or "wordprocessingml" in ct or "docx" in ct:
        return "docx"
    return "txt"


def extract_text_from_docx(docx_bytes: bytes) -> str:
    # python-docx
    try:
        import docx  # type: ignore
        d = docx.Document(io_bytes(docx_bytes))
        parts = []
        for p in d.paragraphs:
            t = (p.text or "").strip()
            if t:
                parts.append(t)
        return "\n".join(parts).strip()
    except Exception:
        return ""


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    # pypdf preferred, PyPDF2 fallback
    try:
        from pypdf import PdfReader  # type: ignore
        reader = PdfReader(io_bytes(pdf_bytes))
        texts = []
        for page in reader.pages:
            t = page.extract_text() or ""
            if t.strip():
                texts.append(t)
        return "\n".join(texts).strip()
    except Exception:
        try:
            from PyPDF2 import PdfReader  # type: ignore
            reader = PdfReader(io_bytes(pdf_bytes))
            texts = []
            for page in reader.pages:
                t = page.extract_text() or ""
                if t.strip():
                    texts.append(t)
            return "\n".join(texts).strip()
        except Exception:
            return ""


def io_bytes(b: bytes):
    import io
    return io.BytesIO(b)


@dataclass
class ExtractResult:
    doc_id: str
    type: str                 # pdf | docx | txt
    text: str                 # analytical text
    pdf_url: Optional[str]    # viewer URL (via /files/…)
    pages: Optional[object]   # keep null for now


async def extract_and_normalize(
    *,
    file_bytes: bytes,
    filename: str,
    content_type: Optional[str],
    storage,
    pdf_store_prefix: str = "docs",
) -> ExtractResult:
    """
    storage must support put_object(key: str, data: bytes, content_type: str|None).
    pdf_url is returned as /files/<key> (backend must serve /files/).
    """
    doc_id = str(uuid.uuid4())
    safe_name = _safe_filename(filename)
    dtype = _guess_type(safe_name, content_type)

    pdf_url: Optional[str] = None
    text: str = ""

    if dtype == "pdf":
        pdf_key = f"{pdf_store_prefix}/{doc_id}.pdf"
        await maybe_await(storage.put_object(pdf_key, file_bytes, content_type="application/pdf"))
        pdf_url = f"/files/{pdf_key}"
        text = extract_text_from_pdf(file_bytes)

    elif dtype == "docx":
        # Convert to PDF first (canonical viewer artifact)
        pdf_bytes = convert_docx_bytes_to_pdf_bytes(file_bytes)
        if pdf_bytes:
            pdf_key = f"{pdf_store_prefix}/{doc_id}.pdf"
            await maybe_await(storage.put_object(pdf_key, pdf_bytes, content_type="application/pdf"))
            pdf_url = f"/files/{pdf_key}"
        # Extract analytical text regardless
        text = extract_text_from_docx(file_bytes)

    else:
        # txt or unknown
        try:
            text = file_bytes.decode("utf-8", errors="replace").strip()
        except Exception:
            text = ""
        pdf_url = None

    return ExtractResult(
        doc_id=doc_id,
        type=dtype,
        text=text,
        pdf_url=pdf_url,
        pages=None,
    )


async def maybe_await(v):
    # Allows StorageProvider implementations to be sync or async.
    if hasattr(v, "__await__"):
        return await v
    return v
