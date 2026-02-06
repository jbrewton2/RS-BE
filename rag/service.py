from __future__ import annotations

import os
import json
import time
from typing import Any, Dict, List, Optional, Tuple

from providers.llm import LLMProvider
from providers.storage import StorageProvider
from providers.vectorstore import VectorStore
from reviews.router import _read_reviews_file  # uses StorageProvider


def _timing_enabled() -> bool:
    return (os.getenv("RAG_TIMING", "") or "").strip().lower() in {"1", "true", "yes", "on"}


def _fast_enabled() -> bool:
    return (os.getenv("RAG_FAST", "") or "").strip().lower() in {"1", "true", "yes", "on"}


def _cap_top_k(top_k: int) -> int:
    # Default cap when RAG_FAST=1
    cap = int((os.getenv("RAG_FAST_TOP_K", "") or "6").strip() or "6")
    return min(int(top_k), cap)


def _cap_context_chars(n: int) -> int:
    cap = int((os.getenv("RAG_FAST_CONTEXT_MAX_CHARS", "") or "8000").strip() or "8000")
    return min(int(n), cap)


def _default_questions() -> List[str]:
    return [
        "What are the key contractual obligations and deliverables across this package?",
        "What language could create cybersecurity or CUI compliance obligations (DFARS/NIST/CMMC)?",
        "What language could create privacy risk?",
        "What language could create legal exposure (data rights, IP, indemnification, disputes)?",
        "What language could create financial uncertainty (funding, pricing, ceilings, cost share)?",
        "What are the major ambiguities, gaps, or questions for the government?",
    ]


def _review_summary_questions_default() -> List[str]:
    # Coverage-oriented queries: designed to reduce ÃƒÂ¢Ã¢â€šÂ¬Ã…â€œunknown/no language providedÃƒÂ¢Ã¢â€šÂ¬Ã‚Â
    return [
        "Mission & objective: What is the purpose, mission, or intended outcome described in this package?",
        "Scope of work: What tasks, requirements, and responsibilities are in scope?",
        "Deliverables & timelines: What deliverables, milestones, deadlines, and period of performance are stated?",
        "Security, compliance & hosting: What requirements are stated (FAR/DFARS/NIST/CMMC/CUI, ATO/IL, logging, encryption, access control, incident reporting, cloud constraints)?",
        "Eligibility & personnel: What citizenship, clearance, export control, staffing, or qualification requirements are stated?",
        "Legal & data rights: What data rights, IP/licensing, indemnification, disputes, warranties, or liability language is stated?",
        "Financial: What pricing model, ROM expectations, ceilings, funding constraints, or payment terms are stated?",
        "Submission instructions: What are submission steps, formats, evaluation criteria, Q&A instructions, and deadlines?",
        "Gaps/ambiguities: What is unclear, contradictory, or missing that we should clarify with the government?",
    ]


def _get_questions() -> List[str]:
    """
    Returns the list of 'domain questions' used for retrieval + synthesis.

    Config:
      - RAG_QUESTIONS_JSON: JSON array of strings.
        Example: ["privacy risk", "legal exposure", "financial uncertainty"]

    If unset/invalid/empty, falls back deterministically.
    """
    raw = (os.getenv("RAG_QUESTIONS_JSON", "") or "").strip()
    if not raw:
        return _default_questions()

    try:
        data = json.loads(raw)
    except Exception:
        # Do NOT hard fail RAG if config is malformed; fall back deterministically.
        return _default_questions()

    if not isinstance(data, list):
        return _default_questions()

    out: List[str] = []
    for item in data:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())

    return out or _default_questions()


def _get_review_summary_questions() -> List[str]:
    """
    Review summary coverage question set.

    Optional config:
      - RAG_REVIEW_SUMMARY_QUESTIONS_JSON: JSON array of strings.

    If unset/invalid/empty, falls back to _review_summary_questions_default().
    """
    raw = (os.getenv("RAG_REVIEW_SUMMARY_QUESTIONS_JSON", "") or "").strip()
    if not raw:
        return _review_summary_questions_default()

    try:
        data = json.loads(raw)
    except Exception:
        return _review_summary_questions_default()

    if not isinstance(data, list):
        return _review_summary_questions_default()

    out: List[str] = []
    for item in data:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())

    return out or _review_summary_questions_default()


def _get_questions_for_mode(mode: Optional[str]) -> List[str]:
    m = (mode or "").strip().lower()
    if m == "review_summary":
        return _get_review_summary_questions()
    # default / chat / unknown -> existing behavior
    return _get_questions()


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
    llm: LLMProvider,
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

        embeddings = llm.embed_texts(chunk_texts)
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
    llm: LLMProvider,
    question: str,
    top_k: int = 12,
    filters: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    q_emb = llm.embed_texts([question])[0]
    return vector.query(query_embedding=q_emb, top_k=int(top_k), filters=filters or {})


def rag_analyze_review(
    *,
    storage: StorageProvider,
    vector: VectorStore,
    llm: LLMProvider,
    review_id: str,
    top_k: int = 12,
    force_reingest: bool = False,
    mode: Optional[str] = None,
) -> Dict[str, Any]:
    """
    True RAG:
      - read docs from StorageProvider (reviews.json)
      - ingest (if requested)
      - retrieve top chunks for a fixed question set
      - synthesize a single executive brief using ONLY retrieved context
    """
    t0 = time.time() if _timing_enabled() else 0.0
    if _timing_enabled():
        print("[RAG] analyze start", review_id, "mode=" + ((mode or "").strip() or "default"))

    if force_reingest:
        t_ing0 = time.time() if _timing_enabled() else 0.0
        ingest_review_docs(storage=storage, vector=vector, llm=llm, review_id=review_id)
        if _timing_enabled():
            print("[RAG] ingest done", round(time.time() - t_ing0, 2), "s")

    questions = _get_questions_for_mode(mode)

    effective_top_k = _cap_top_k(top_k) if _fast_enabled() else int(top_k)

    t_ret0 = time.time() if _timing_enabled() else 0.0
    retrieved: Dict[str, List[Dict[str, Any]]] = {}
    citations: List[Dict[str, Any]] = []

    for q in questions:
        hits = query_review(
            vector=vector,
            llm=llm,
            question=q,
            top_k=effective_top_k,
            filters={"review_id": review_id},
        )
        retrieved[q] = hits

    if _timing_enabled():
        print("[RAG] retrieval done", round(time.time() - t_ret0, 2), "s")

    def fmt_hit(h: Dict[str, Any]) -> str:
        meta = h.get("meta") or {}
        doc = meta.get("doc_name") or h.get("doc_name") or meta.get("doc_id") or "UnknownDoc"
        cs = meta.get("char_start")
        ce = meta.get("char_end")
        score = h.get("score")
        chunk_text = (h.get("chunk_text") or "").strip()
        snippet = chunk_text[:900]
        return f"===BEGIN CONTRACT EVIDENCE===\nDOC: {doc} | score={score} | span={cs}-{ce}\n{snippet}\n===END CONTRACT EVIDENCE==="

    blocks: List[str] = []
    for q in questions:
        blocks.append(
            f"QUESTION: {q}\nRETRIEVED EVIDENCE:\n" + "\n".join(fmt_hit(h) for h in retrieved[q][:effective_top_k])
        )

    context = "\n\n".join(blocks)

    # Hard bound for stability (can be tuned without code changes)
    max_chars = int((os.getenv("RAG_CONTEXT_MAX_CHARS", "") or "16000").strip() or "16000")
    if _fast_enabled():
        max_chars = _cap_context_chars(max_chars)
    context = context[:max_chars]

    # Prompt varies by mode. review_summary is stricter about evidence & gaps.
    m = (mode or "").strip().lower()
    if m == "review_summary":
        prompt = f"""
OVERVIEW
Write ONE unified cross-document executive brief for this review.

HARD RULES (REVIEW SUMMARY MODE)
- Plain text only. No markdown.
- Do NOT output bracket placeholders like "[insert ...]" or any templating filler.
- If you cannot find evidence for a section, write: "Insufficient evidence retrieved for this section."
- Do not fabricate deliverables, dates, roles, responsibilities, or requirements.
- You MUST use evidence from the retrieved context only.
- Evidence MUST be copied only from within blocks between:
  ===BEGIN CONTRACT EVIDENCE=== and ===END CONTRACT EVIDENCE===
- Do NOT treat the QUESTION lines, headings, or instructions as evidence.
- For EACH SECTION, include:
  1) Findings (bullets)
  2) Evidence: 1ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Å“3 short snippets from retrieved context (copy exact phrases).
- If a section has insufficient evidence, write:
  "Insufficient evidence retrieved for this section." and list what should be retrieved/clarified.
- Do NOT say ÃƒÂ¢Ã¢â€šÂ¬Ã…â€œunknownÃƒÂ¢Ã¢â€šÂ¬Ã‚Â without explicitly calling out the missing evidence as a coverage gap.

SECTIONS (exact order)
OVERVIEW
MISSION & OBJECTIVE
SCOPE OF WORK
DELIVERABLES & TIMELINES
SECURITY, COMPLIANCE & HOSTING CONSTRAINTS
ELIGIBILITY & PERSONNEL CONSTRAINTS
LEGAL & DATA RIGHTS RISKS
FINANCIAL RISKS
SUBMISSION INSTRUCTIONS & DEADLINES
CONTRADICTIONS & INCONSISTENCIES
GAPS / QUESTIONS FOR THE GOVERNMENT
RECOMMENDED INTERNAL ACTIONS

RETRIEVED CONTEXT
{context}
""".strip()
    else:
        prompt = f"""
OVERVIEW
Write ONE unified cross-document executive brief for this review.

RULES
- Plain text only. No markdown.
- Do NOT output bracket placeholders like "[insert ...]" or any templating filler.
- If you cannot find evidence for a section, write: "Insufficient evidence retrieved for this section."
- Do not fabricate deliverables, dates, roles, responsibilities, or requirements.
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

    t_gen0 = time.time() if _timing_enabled() else 0.0
    if _timing_enabled():
        print("[RAG] generation start")

    summary = (llm.generate(prompt) or {}).get("text") or ""

    if _timing_enabled():
        print("[RAG] generation done", round(time.time() - t_gen0, 2), "s")

    for q in questions:
        for h in retrieved[q][: min(3, int(effective_top_k))]:
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

    if _timing_enabled():
        print("[RAG] analyze done", round(time.time() - t0, 2), "s")

    return {
        "review_id": review_id,
        "summary": summary,
        "citations": citations,
        "retrieved_counts": {q: len(retrieved[q]) for q in questions},
    }
