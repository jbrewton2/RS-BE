from __future__ import annotations

from typing import Any, Dict, List, Tuple


def effective_top_k(req_top_k: int, context_profile: str) -> int:
    # Mirror current service.py behavior; keep conservative and deterministic
    p = (context_profile or "fast").strip().lower()
    k = int(req_top_k or 0)
    if k <= 0:
        k = 1
    if p == "fast":
        return min(max(k, 1), 4)
    if p == "deep":
        return min(max(k, 8), 20)
    # standard/balanced
    return min(max(k, 4), 12)


def effective_context_chars(context_profile: str) -> int:
    p = (context_profile or "fast").strip().lower()
    if p == "fast":
        return 16000
    if p == "deep":
        return 80000
    return 32000


def effective_snippet_chars(context_profile: str) -> int:
    p = (context_profile or "fast").strip().lower()
    if p == "fast":
        return 900
    if p == "deep":
        return 2200
    return 1400


def retrieve_context_local(
    *,
    vector: Any,
    llm: Any,
    questions: List[str],
    review_id: str,
    effective_top_k: int,
    snippet_cap: int,
    context_cap: int,
    debug: bool,
) -> Tuple[Dict[str, List[Dict[str, Any]]], str, Dict[str, int], List[Dict[str, Any]]]:
    """
    Local retrieval helper (vector.query + llm.embed_texts).

    Returns:
      (retrieved_hits_by_question, assembled_context_str, retrieved_counts, retrieval_debug)
    """
    retrieved: Dict[str, List[Dict[str, Any]]] = {}
    retrieved_counts: Dict[str, int] = {}
    retrieval_debug: List[Dict[str, Any]] = []

    if not questions:
        return {}, "", {}, []

    if not hasattr(llm, "embed_texts"):
        raise RuntimeError("LLM provider does not implement embed_texts() required for retrieval")

    embs = llm.embed_texts(list(questions))
    if not isinstance(embs, list) or len(embs) != len(questions):
        raise RuntimeError("embed_texts returned unexpected embeddings count")

    for q, emb in zip(questions, embs):
        try:
            hits = vector.query(emb, top_k=effective_top_k, filters={"review_id": str(review_id)})
        except Exception as e:
            hits = []
            if debug:
                retrieval_debug.append({"q": q, "error": repr(e)})

        retrieved[q] = hits or []
        retrieved_counts[q] = len(hits or [])

        if debug:
            retrieval_debug.append(
                {
                    "q": q,
                    "hits": len(hits or []),
                    "top": [
                        {
                            "doc_name": (h.get("doc_name") or ""),
                            "chunk_id": (h.get("chunk_id") or ""),
                            "score": h.get("score"),
                        }
                        for h in (hits or [])[:3]
                    ],
                }
            )

    ctx_parts: List[str] = []
    used = 0

    for q in questions:
        hits = retrieved.get(q) or []
        if not hits:
            continue

        hdr = f"Q: {q}\n"
        if used + len(hdr) > context_cap:
            break
        ctx_parts.append(hdr)
        used += len(hdr)

        per_q = min(max(effective_top_k, 8), 20)

        # If question count is high, treat as triage-like context pressure.
        if str(review_id) and isinstance(questions, list) and len(questions) >= 15:
            per_q = min(max(effective_top_k, 4), 8)

        for h in hits[:per_q]:
            txt = (h.get("chunk_text") or "").strip()
            if not txt:
                continue
            if snippet_cap > 0 and len(txt) > snippet_cap:
                txt = txt[:snippet_cap].rstrip() + "..."

            meta = h.get("meta") or {}
            doc = meta.get("doc_name") or h.get("doc_name") or meta.get("doc_id") or h.get("document_id") or "doc"
            cid = h.get("chunk_id") or ""
            line = f"- ({doc} / {cid}) {txt}\n"
            if used + len(line) > context_cap:
                break
            ctx_parts.append(line)
            used += len(line)

        ctx_parts.append("\n")
        used += 1
        if used >= context_cap:
            break

    context = "".join(ctx_parts).strip()
    return retrieved, context, retrieved_counts, retrieval_debug
