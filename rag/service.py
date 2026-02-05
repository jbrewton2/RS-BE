from __future__ import annotations

import os
import requests
from typing import Any, Dict, List, Optional, Tuple

from providers.storage import StorageProvider
from providers.vectorstore import VectorStore
from reviews.router import _read_reviews_file  # uses StorageProvider


def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return default if v is None else str(v)


def _split_scopes(value: str) -> List[str]:
    raw = (value or "").replace(",", " ").strip()
    return [x.strip() for x in raw.split() if x.strip()]


def chunk_text(text: str, chunk_size: int = 1500, overlap: int = 250) -> List[Tuple[int, int, str]]:
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
    Local embeddings via Ollama.
    Swap later by changing env vars + provider impl (Bedrock embedding model etc.)
    """
    base = _env("OLLAMA_BASE_URL", "").strip()
    if not base:
        api_url = _env("OLLAMA_API_URL", "http://ollama:11434/api/chat").strip()
        base = api_url.split("/api/")[0].rstrip("/")

    model = _env("EMBEDDING_MODEL", "nomic-embed-text").strip()
    url = f"{base}/api/embeddings"

    vectors: List[List[float]] = []
    for t in texts:
        payload = {"model": model, "prompt": t}
        r = requests.post(url, json=payload, timeout=60)
        r.raise_for_status()
        data = r.json()
        emb = data.get("embedding") or []
        vectors.append([float(x) for x in emb])
    return vectors


def _ollama_generate(prompt: str) -> str:
    """
    Uses your existing OLLAMA_API_URL (chat) settings.
    """
    api_url = _env("OLLAMA_API_URL", "http://ollama:11434/api/chat").strip()
    model = _env("OLLAMA_MODEL", "llama3.1").strip()
    timeout = float(_env("OLLAMA_TIMEOUT_SECONDS", "240") or "240")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a contract and risk analyst. Return plain text only."},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "options": {"temperature": float(_env("LLM_TEMPERATURE", "0.2") or "0.2")},
    }

    r = requests.post(api_url, json=payload, timeout=timeout)
    r.raise_for_status()
    data = r.json()

    # Ollama chat commonly returns: {"message":{"role":"assistant","content":"..."}, ...}
    msg = (data.get("message") or {})
    content = (msg.get("content") or "").strip()
    return content


def _get_review(storage: StorageProvider, review_id: str) -> Dict[str, Any]:
    reviews = _read_reviews_file(storage)
    review = next((r for r in reviews if r.get("id") == review_id), None)
    if not review:
        raise KeyError("review_not_found")
    return review


def ingest_review_docs(
    *,
    storage: StorageProvider,
    vector: VectorStore,
    review_id: str,
    chunk_size: int = 1500,
    overlap: int = 250,
) -> Dict[str, Any]:
    review = _get_review(storage, review_id)
    docs = (review.get("docs") or []) if isinstance(review.get("docs"), list) else []

    total_chunks = 0
    doc_results: List[Dict[str, Any]] = []

    for d in docs:
        doc_id = (d.get("id") or "").strip()
        doc_name = (d.get("name") or "").strip() or None
        text = (d.get("text") or d.get("content") or "").strip()

        if not doc_id or not text:
            doc_results.append({"doc_id": doc_id or None, "doc_name": doc_name, "chunks": 0, "skipped": True})
            continue

        chunks = chunk_text(text, chunk_size=chunk_size, overlap=overlap)
        if not chunks:
            doc_results.append({"doc_id": doc_id, "doc_name": doc_name, "chunks": 0, "skipped": True})
            continue

        payloads: List[Dict[str, Any]] = []
        chunk_texts: List[str] = []

        for idx, (cs, ce, ct) in enumerate(chunks):
            cid = f"{doc_id}:{idx}:{cs}:{ce}"
            payloads.append(
                {
                    "chunk_id": cid,
                    "doc_name": doc_name,
                    "chunk_text": ct,
                    "meta": {
                        "review_id": review_id,
                        "doc_id": doc_id,
                        "doc_name": doc_name,
                        "char_start": cs,
                        "char_end": ce,
                        "chunk_index": idx,
                    },
                }
            )
            chunk_texts.append(ct)

        embeddings = _ollama_embed(chunk_texts)
        for i in range(min(len(payloads), len(embeddings))):
            payloads[i]["embedding"] = embeddings[i]

        vector.upsert_chunks(document_id=doc_id, chunks=payloads)
        total_chunks += len(payloads)
        doc_results.append({"doc_id": doc_id, "doc_name": doc_name, "chunks": len(payloads), "skipped": False})

    return {
        "review_id": review_id,
        "documents": len(docs),
        "total_chunks_upserted": total_chunks,
        "per_document": doc_results,
    }


def query_review(
    *,
    vector: VectorStore,
    question: str,
    top_k: int = 12,
    filters: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    q_emb = _ollama_embed([question])[0]
    return vector.query(query_embedding=q_emb, top_k=int(top_k), filters=filters or {})


def rag_analyze_review(
    *,
    storage: StorageProvider,
    vector: VectorStore,
    review_id: str,
    top_k: int = 12,
    force_reingest: bool = False,
) -> Dict[str, Any]:
    """
    True RAG:
      - read docs from StorageProvider (reviews.json in MinIO today)
      - ingest (if requested)
      - retrieve top chunks for a fixed question set
      - synthesize a single executive brief using ONLY retrieved context
    """
    if force_reingest:
        ingest_review_docs(storage=storage, vector=vector, review_id=review_id)

    # These are your “domain queries” that drive retrieval.
    # Expand/tune without changing the API.
    questions = [
        "What are the key contractual obligations and deliverables across this package?",
        "What language could create cybersecurity or CUI compliance obligations (DFARS/NIST/CMMC)?",
        "What language could create privacy risk?",
        "What language could create legal exposure (data rights, IP, indemnification, disputes)?",
        "What language could create financial uncertainty (funding, pricing, ceilings, cost share)?",
        "What are the major ambiguities, gaps, or questions for the government?",
    ]

    retrieved: Dict[str, List[Dict[str, Any]]] = {}
    citations: List[Dict[str, Any]] = []

    for q in questions:
        hits = query_review(vector=vector, question=q, top_k=top_k, filters={"review_id": review_id})
        retrieved[q] = hits

    # Build bounded context
    def fmt_hit(h: Dict[str, Any]) -> str:
        meta = h.get("meta") or {}
        doc = meta.get("doc_name") or h.get("doc_name") or meta.get("doc_id") or "UnknownDoc"
        cs = meta.get("char_start")
        ce = meta.get("char_end")
        score = h.get("score")
        chunk_text = (h.get("chunk_text") or "").strip()
        snippet = chunk_text[:900]
        return f"- DOC: {doc} | score={score}\n  span={cs}-{ce}\n  {snippet}"

    blocks: List[str] = []
    for q in questions:
        blocks.append(f"QUESTION: {q}\nRETRIEVED EVIDENCE:\n" + "\n".join(fmt_hit(h) for h in retrieved[q][:top_k]))

    context = "\n\n".join(blocks)
    context = context[:16000]  # hard bound for local stability

    prompt = f"""
OVERVIEW
Write ONE unified cross-document executive brief for this review.

RULES
- Plain text only. No markdown.
- Use evidence from the retrieved context only.
- If something is unknown, say so and list it under GAPS / QUESTIONS FOR THE GOVERNMENT.

SECTIONS (exact order)
OVERVIEW
MISSION & OBJECTIVE
SCOPE OF WORK
DELIVERABLES & TIMELINES
SECURITY, COMPLIANCE & HOSTING CONSTRAINTS
LEGAL & DATA RIGHTS RISKS
FINANCIAL RISKS
CONTRADICTIONS & INCONSISTENCIES
GAPS / QUESTIONS FOR THE GOVERNMENT
RECOMMENDED INTERNAL ACTIONS

RETRIEVED CONTEXT
{context}
""".strip()

    summary = _ollama_generate(prompt)

    # Minimal citations: aggregate top hits per question
    for q in questions:
        for h in retrieved[q][: min(3, top_k)]:
            meta = h.get("meta") or {}
            citations.append(
                {
                    "question": q,
                    "doc": meta.get("doc_name") or meta.get("doc_id"),
                    "docId": meta.get("doc_id"),
                    "charStart": meta.get("char_start"),
                    "charEnd": meta.get("char_end"),
                    "score": h.get("score"),
                }
            )

    return {
        "review_id": review_id,
        "summary": summary,
        "citations": citations,
        "retrieved_counts": {q: len(retrieved[q]) for q in questions},
    }
