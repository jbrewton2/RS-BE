from __future__ import annotations

"""rag/ingestion_engine.py

Single source of truth for document text ingestion into the vector store.

Design goals:
- Deterministic, testable logic.
- No provider resolution here (providers are passed in).
- One canonical contract for doc objects:
  - doc_id (or id)
  - name/filename/title (optional)
  - pdf_url (optional) for DOCX->PDF extracts

Storage contract (S3-compatible StorageProvider):
- Prefer pre-extracted raw text if present:
    {S3_PREFIX}/review_pdfs/extract/{doc_id}/raw_text.txt
- Fallback to a PDF stored in:
    {S3_PREFIX}/review_pdfs/{doc_id}.pdf
- Optional fallback: fetch PDF bytes from `pdf_url` (requires Authorization bearer token).

Feature flags:
- RAG_PDFURL_FETCH_ENABLED (default 0)
    If 1, ingestion may fetch PDFs from pdf_url when raw_text/PDF not found in storage.

"""

from dataclasses import dataclass
from io import BytesIO
import hashlib
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

import requests

from core.config import PdfReader
from providers.storage import StorageProvider
from providers.vectorstore import VectorStore

log = logging.getLogger(__name__)


def _env_bool(name: str, default: str = "0") -> bool:
    try:
        return (os.environ.get(name, default) or default).strip().lower() in ("1", "true", "yes", "y", "on")
    except Exception:
        return False


def _s3k(p: str) -> str:
    # IMPORTANT: StorageProvider (storage_s3.py) applies S3_PREFIX.
    # Ingestion must pass RELATIVE keys only (no prefix here).
    return (p or "").lstrip("/").strip()


def _extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    if PdfReader is None:
        return ""
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        texts: List[str] = []
        for _page_idx, page in enumerate(reader.pages, start=1):
            try:
                page_text = page.extract_text() or ""
            except Exception:
                page_text = ""
            if page_text.strip():
                texts.append(page_text)
        return "\n".join(texts).strip()
    except Exception:
        return ""


def _chunk_text_windowed(text: str, *, chunk_size: int = 1400, overlap: int = 200) -> List[Dict[str, Any]]:
    t = (text or "").strip()
    if not t:
        return []

    chunk_size = max(200, int(chunk_size))
    overlap = max(0, min(int(overlap), chunk_size - 1))

    out: List[Dict[str, Any]] = []
    i = 0
    start = 0
    while start < len(t):
        end = min(len(t), start + chunk_size)
        chunk_text = t[start:end].strip()
        if chunk_text:
            out.append(
                {
                    "chunk_id": f"{i}:{start}:{end}",
                    "chunk_text": chunk_text,
                    "meta": {"char_start": start, "char_end": end, "chunk_index": i},
                }
            )
            i += 1
        if end >= len(t):
            break
        start = max(0, end - overlap)

    return out


def _read_extracted_text_for_doc(
    storage: StorageProvider,
    *,
    doc_id: str,
    pdf_url: str = "",
    token: str = "",
) -> str:
    """Return best-effort extracted text for a doc.

    Order:
    1) raw_text.txt in storage
    2) (optional) pdf_url fetch
    3) stored PDF bytes

    Never raises; returns "" on failure.
    """

    doc_id = (doc_id or "").strip()
    if not doc_id:
        return ""

    # 1) raw text artifact (preferred)
    extract_key = f"review_pdfs/extract/{doc_id}/raw_text.txt"
    extract_key_legacy = f"extract/{doc_id}/raw_text.txt"
    try:
        b = storage.get_object(key=extract_key)
        if isinstance(b, (bytes, bytearray)):
            t = bytes(b).decode("utf-8", errors="ignore").strip()
            if t:
                return t
    except Exception:
        pass

    # Legacy extract fallback (current S3 has raw_text here)
    try:
        b = storage.get_object(key=extract_key_legacy)
        if isinstance(b, (bytes, bytearray)):
            t = bytes(b).decode("utf-8", errors="ignore").strip()
            if t:
                return t
    except Exception:
        pass


    # 2) pdf_url fallback (feature-flagged)
    pdf_url = (pdf_url or "").strip()
    if pdf_url and _env_bool("RAG_PDFURL_FETCH_ENABLED", "1"):
        try:
            headers: Dict[str, str] = {}
            tok = (token or "").strip()
            if tok:
                headers["Authorization"] = f"Bearer {tok}"
            r = requests.get(pdf_url, headers=headers, timeout=30)
            # stdout logging for k8s visibility
            print(f"pdf_url_fetch doc_id={doc_id} has_token={bool(tok)} status={getattr(r, 'status_code', None)} bytes={len(getattr(r, 'content', b'') or b'')} url={pdf_url}")
            if r.status_code == 200 and r.content:
                t = _extract_text_from_pdf_bytes(bytes(r.content))
                if t.strip():
                    return t.strip()
        except Exception as e:
            print(f"pdf_url_fetch_error doc_id={doc_id} has_token={bool((token or '').strip())} err={repr(e)} url={pdf_url}")

    # 3) stored PDF fallback
    pdf_key = f"review_pdfs/{doc_id}.pdf"
    try:
        pdf_bytes = storage.get_object(key=pdf_key)
    except Exception:
        pdf_bytes = None

    if not isinstance(pdf_bytes, (bytes, bytearray)) or not pdf_bytes:
        return ""

    text = _extract_text_from_pdf_bytes(bytes(pdf_bytes))
    if not text.strip():
        return ""

    # Best-effort write-back
    try:
        raw_text_bytes = text.encode("utf-8", errors="ignore")
        extract_json_key = f"review_pdfs/extract/{doc_id}/extract.json"
        payload = {
            "doc_id": doc_id,
            "pdf_key": pdf_key,
            "pdf_sha256": hashlib.sha256(bytes(pdf_bytes)).hexdigest(),
            "extract_text_sha256": hashlib.sha256(raw_text_bytes).hexdigest(),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        storage.put_object(
            key=extract_key,
            data=raw_text_bytes,
            content_type="text/plain; charset=utf-8",
            metadata=None,
        )
        storage.put_object(
            key=extract_json_key,
            data=json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8", errors="ignore"),
            content_type="application/json",
            metadata=None,
        )
    except Exception:
        pass

    return text.strip()


def _ingest_review_into_vectorstore(
    *,
    storage: StorageProvider,
    llm: Any,
    vector: VectorStore,
    docs: List[Dict[str, Any]],
    review_id: str,
    profile: str,
    token: str = "",
) -> Dict[str, Any]:
    """Ingest a review's docs into the vector store."""

    if not isinstance(docs, list) or not docs:
        return {"ingested_docs": 0, "ingested_chunks": 0, "skipped_docs": 0, "reason": "no_docs"}

    p = (profile or "").lower().strip()
    chunk_size = 1400 if p == "deep" else (1000 if p == "balanced" else 900)
    overlap = 200

    ingested_docs = 0
    ingested_chunks = 0
    skipped_docs = 0

    for d in docs:
        if not isinstance(d, dict):
            skipped_docs += 1
            continue

        doc_id = (d.get("doc_id") or d.get("id") or "").strip()
        if not doc_id:
            skipped_docs += 1
            continue

        doc_name = (d.get("name") or d.get("filename") or d.get("title") or f"review:{review_id}").strip()
        pdf_url = (d.get("pdf_url") or "").strip()

        raw_text = _read_extracted_text_for_doc(storage, doc_id=doc_id, pdf_url=pdf_url, token=token)
        if not raw_text:
            skipped_docs += 1
            continue

        chunks = _chunk_text_windowed(raw_text, chunk_size=chunk_size, overlap=overlap)
        if not chunks:
            skipped_docs += 1
            continue

        if not hasattr(llm, "embed_texts"):
            raise RuntimeError("LLM provider does not implement embed_texts() required for vector ingest")

        texts = [c["chunk_text"] for c in chunks]
        embeddings = llm.embed_texts(texts)

        if not isinstance(embeddings, list) or len(embeddings) != len(chunks):
            raise RuntimeError("embed_texts returned unexpected number of embeddings")

        upsert_payload: List[Dict[str, Any]] = []
        for c, emb in zip(chunks, embeddings):
            meta = c.get("meta") or {}
            meta = dict(meta) if isinstance(meta, dict) else {}
            meta.update({"review_id": str(review_id), "doc_id": str(doc_id), "doc_name": str(doc_name)})

            upsert_payload.append(
                {
                    "review_id": str(review_id),
                    "chunk_id": str(c.get("chunk_id") or ""),
                    "chunk_text": str(c.get("chunk_text") or ""),
                    "doc_name": str(doc_name),
                    "meta": meta,
                    "embedding": emb,
                }
            )

        # Replace existing doc vectors
        vector.delete_by_document(str(doc_id))
        vector.upsert_chunks(document_id=str(doc_id), chunks=upsert_payload, review_id=review_id)

        ingested_docs += 1
        ingested_chunks += len(upsert_payload)

    return {"ingested_docs": ingested_docs, "ingested_chunks": ingested_chunks, "skipped_docs": skipped_docs}
