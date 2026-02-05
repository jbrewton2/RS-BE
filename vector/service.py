from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

import httpx

from providers.vectorstore import VectorStore


def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return default if v is None else str(v)


def chunk_text(text: str, chunk_size: int = 1500, overlap: int = 250) -> List[Tuple[int, int, str]]:
    """
    Deterministic char-chunking. Good enough for Phase 1 local RAG.
    Later: token chunking can replace this without changing VectorStore API.
    """
    t = (text or "")
    if not t.strip():
        return []

    chunk_size = max(200, int(chunk_size))
    overlap = max(0, min(int(overlap), chunk_size - 1))

    out: List[Tuple[int, int, str]] = []
    start = 0
    n = len(t)

    while start < n:
        end = min(n, start + chunk_size)
        out.append((start, end, t[start:end]))
        if end >= n:
            break
        start = max(0, end - overlap)

    return out


def _ollama_embed(texts: List[str]) -> List[List[float]]:
    """
    Uses Ollama embeddings API.
    - base: OLLAMA_BASE_URL (default derived from OLLAMA_API_URL)
    - model: EMBEDDING_MODEL (default nomic-embed-text)
    """
    base = _env("OLLAMA_BASE_URL", "").strip()
    if not base:
        api_url = _env("OLLAMA_API_URL", "http://ollama:11434/api/chat").strip()
        base = api_url.split("/api/")[0].rstrip("/")

    model = _env("EMBEDDING_MODEL", "nomic-embed-text").strip()
    url = f"{base}/api/embeddings"

    timeout = float(_env("EMBED_TIMEOUT_SECONDS", "60") or "60")

    vectors: List[List[float]] = []
    with httpx.Client(timeout=timeout) as client:
        for t in texts:
            payload = {"model": model, "prompt": t}
            r = client.post(url, json=payload)
            r.raise_for_status()
            data = r.json()
            emb = data.get("embedding") or []
            vectors.append([float(x) for x in emb])

    return vectors


def ingest_document(
    vector: VectorStore,
    document_id: str,
    doc_name: Optional[str],
    text: str,
    chunk_size: int = 1500,
    overlap: int = 250,
) -> Dict[str, Any]:
    chunks = chunk_text(text, chunk_size=chunk_size, overlap=overlap)
    if not chunks:
        return {"document_id": document_id, "chunks": 0}

    chunk_payloads: List[Dict[str, Any]] = []
    chunk_texts: List[str] = []

    for idx, (cs, ce, ct) in enumerate(chunks):
        cid = f"{document_id}:{idx}:{cs}:{ce}"
        chunk_texts.append(ct)
        chunk_payloads.append(
            {
                "chunk_id": cid,
                "doc_name": doc_name,
                "chunk_text": ct,
                "meta": {
                    "char_start": cs,
                    "char_end": ce,
                    "chunk_index": idx,
                },
            }
        )

    embeddings = _ollama_embed(chunk_texts)

    # Attach embeddings
    for i in range(min(len(chunk_payloads), len(embeddings))):
        chunk_payloads[i]["embedding"] = embeddings[i]

    vector.upsert_chunks(document_id=document_id, chunks=chunk_payloads)
    return {"document_id": document_id, "chunks": len(chunk_payloads)}


def query(
    vector: VectorStore,
    question: str,
    top_k: int = 10,
    filters: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    q_emb = _ollama_embed([question])[0]
    return vector.query(query_embedding=q_emb, top_k=top_k, filters=filters or {})
