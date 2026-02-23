from __future__ import annotations

# NOTE:
# This module was split out of rag/service.py for maintainability.
# Keep it deterministic and testable. Avoid provider resolution here.

from typing import Any, Dict, List, Optional

# Import the provider protocols used by signatures
from providers.storage import StorageProvider
from providers.vectorstore import VectorStore

# Standard libs commonly used by ingestion (safe even if unused)
import io
import re


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

def _extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    if PdfReader is None:
        return ""
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        texts: List[str] = []
        for page in reader.pages:
            try:
                page_text = page.extract_text() or ""
            except Exception:
                page_text = ""
            if page_text.strip():
                texts.append(page_text)
        return "\n".join(texts).strip()
    except Exception:
        return ""

def _read_extracted_text_for_doc(storage: StorageProvider, *, doc_id: str) -> str:
    doc_id = (doc_id or "").strip()
    if not doc_id:
        return ""
    extract_key = f"extract/{doc_id}/raw_text.txt"

    try:
        b = storage.get_object(key=extract_key)
        if isinstance(b, (bytes, bytearray)):
            t = b.decode("utf-8", errors="ignore").strip()
            if t:
                return t
    except Exception:
        pass

    pdf_key = f"review_pdfs/{doc_id}.pdf"
    try:
        pdf_bytes = storage.get_object(key=pdf_key)
    except Exception:
        pdf_bytes = None

    if not isinstance(pdf_bytes, (bytes, bytearray)) or not pdf_bytes:
        return ""

    text = _extract_text_from_pdf_bytes(bytes(pdf_bytes))
    if not text:
        return ""

    try:
        raw_text_bytes = text.encode("utf-8", errors="ignore").strip()
        extract_json_key = f"extract/{doc_id}/extract.json"
        payload = {
            "doc_id": doc_id,
            "pdf_key": pdf_key,
            "pdf_sha256": hashlib.sha256(bytes(pdf_bytes)).hexdigest(),
            "extract_text_sha256": hashlib.sha256(raw_text_bytes).hexdigest(),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        extract_json_bytes = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8", errors="ignore")
        storage.put_object(key=extract_key, data=raw_text_bytes, content_type="text/plain; charset=utf-8", metadata=None)
        storage.put_object(key=extract_json_key, data=extract_json_bytes, content_type="application/json", metadata=None)
    except Exception:
        pass

    return text

def _ingest_review_into_vectorstore(
    *,
    storage: StorageProvider,
    llm: Any,
    vector: VectorStore,
    docs: List[Dict[str, Any]],
    review_id: str,
    profile: str,
) -> Dict[str, Any]:
    if not isinstance(docs, list) or not docs:
        return {"ingested_docs": 0, "ingested_chunks": 0, "skipped_docs": 0, "reason": "no_docs"}

    p = (profile or "").lower()
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
        raw_text = _read_extracted_text_for_doc(storage, doc_id=doc_id)
        if not raw_text:
            skipped_docs += 1
            continue

        chunks = _chunk_text_windowed(raw_text, chunk_size=chunk_size, overlap=overlap)
        if not chunks:
            skipped_docs += 1
            continue

        texts = [c["chunk_text"] for c in chunks]
        if not hasattr(llm, "embed_texts"):
            raise RuntimeError("LLM provider does not implement embed_texts() required for vector ingest")
        embeddings = llm.embed_texts(texts)

        if not isinstance(embeddings, list) or len(embeddings) != len(chunks):
            raise RuntimeError("embed_texts returned unexpected number of embeddings")

        upsert_payload: List[Dict[str, Any]] = []
        for c, emb in zip(chunks, embeddings):
            meta = c.get("meta") or {}
            meta = dict(meta) if isinstance(meta, dict) else {}
            meta["review_id"] = str(review_id)
            meta["doc_id"] = str(doc_id)
            meta["doc_name"] = str(doc_name)
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

        vector.delete_by_document(str(doc_id))
        vector.upsert_chunks(document_id=str(doc_id), chunks=upsert_payload, review_id=review_id)

        ingested_docs += 1
        ingested_chunks += len(upsert_payload)

    return {"ingested_docs": ingested_docs, "ingested_chunks": ingested_chunks, "skipped_docs": skipped_docs}


# =============================================================================
# LLM call helper
# =============================================================================
