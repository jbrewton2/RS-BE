# rag/service.py
from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from providers.storage import StorageProvider
from providers.vectorstore import VectorStore
from reviews.router import _read_reviews_file  # uses StorageProvider


# -----------------------------
# Contract: modes + defaults
# -----------------------------
RAG_MODE_REVIEW_SUMMARY = "review_summary"
RAG_MODE_DEFAULT = RAG_MODE_REVIEW_SUMMARY

RAG_ALLOWED_MODES = {
    RAG_MODE_REVIEW_SUMMARY,
    "default",  # backward compat
}

RAG_REVIEW_SUMMARY_SECTIONS: List[str] = [
    "OVERVIEW",
    "MISSION & OBJECTIVE",
    "SCOPE OF WORK",
    "DELIVERABLES & TIMELINES",
    "SECURITY, COMPLIANCE & HOSTING CONSTRAINTS",
    "ELIGIBILITY & PERSONNEL CONSTRAINTS",
    "LEGAL & DATA RIGHTS RISKS",
    "FINANCIAL RISKS",
    "SUBMISSION INSTRUCTIONS & DEADLINES",
    "CONTRADICTIONS & INCONSISTENCIES",
    "GAPS / QUESTIONS FOR THE GOVERNMENT",
    "RECOMMENDED INTERNAL ACTIONS",
]

INSUFFICIENT = "Insufficient evidence retrieved for this section."


def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return default if v is None else str(v)


def _timing_enabled() -> bool:
    return (_env("RAG_TIMING", "0").strip() == "1")


def _fast_enabled() -> bool:
    # NOTE: you wanted RAG_FAST=1 by default going forward.
    return (_env("RAG_FAST", "0").strip() == "1")


def _normalize_text(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = s.replace("\x00", "")
    s = s.replace("\u2013", "-").replace("\u2014", "-")
    s = s.replace("\u2018", "'").replace("\u2019", "'")
    s = s.replace("\u201c", '"').replace("\u201d", '"')
    return s


_PLACEHOLDER_PATTERNS = [
    r"\[insert[^\]]*\]",
    r"\(\"\.\.\.\"\)",
    r"\.\.\.\s*$",
]


def _strip_placeholders(s: str) -> str:
    if not s:
        return ""
    out = s
    for p in _PLACEHOLDER_PATTERNS:
        out = re.sub(p, "", out, flags=re.IGNORECASE | re.MULTILINE)
    return out


def _strip_markdown_headers(s: str) -> str:
    out_lines: List[str] = []
    for line in _normalize_text(s).split("\n"):
        out_lines.append(re.sub(r"^\s{0,3}#{1,6}\s*", "", line))
    return "\n".join(out_lines)


def _collapse_blank_lines(s: str) -> str:
    s = _normalize_text(s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip() + "\n"


def _split_sections(text: str) -> Dict[str, str]:
    lines = _normalize_text(text).split("\n")
    sections: Dict[str, List[str]] = {}
    current: Optional[str] = None
    header_set = set(RAG_REVIEW_SUMMARY_SECTIONS)

    for raw in lines:
        line = raw.strip()
        if line in header_set:
            current = line
            sections[current] = []
            continue
        if current is not None:
            sections[current].append(raw)

    return {k: "\n".join(v).strip() for k, v in sections.items()}


def _render_sections_in_order(sections: Dict[str, str], order: List[str]) -> str:
    out: List[str] = []
    for h in order:
        body = (sections.get(h) or "").strip()
        if not body:
            body = INSUFFICIENT
        out.append(h)
        out.append(body)
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _postprocess_review_summary(text: str) -> str:
    text = _strip_markdown_headers(text)
    text = _strip_placeholders(text)

    parsed = _split_sections(text)
    if not parsed:
        parsed = {"OVERVIEW": text.strip() or INSUFFICIENT}

    hardened = _render_sections_in_order(parsed, RAG_REVIEW_SUMMARY_SECTIONS)
    return _collapse_blank_lines(hardened)


def _canonical_mode(mode: Optional[str]) -> str:
    m = (mode or "").strip().lower()
    if not m:
        return RAG_MODE_DEFAULT
    if m == "default":
        return RAG_MODE_DEFAULT
    if m not in RAG_ALLOWED_MODES:
        raise ValueError(f"Unsupported RAG mode: {m}. Allowed: {sorted(RAG_ALLOWED_MODES)}")
    return m


# -----------------------------
# Profile-driven caps
# -----------------------------
def _effective_top_k(req_top_k: int, context_profile: str) -> int:
    """
    Compute top_k based on request + context_profile.
    IMPORTANT: deep/balanced override fast caps (RAG_FAST) to allow real triage.
    """
    k = max(1, min(int(req_top_k or 12), 50))
    p = (context_profile or "fast").strip().lower()

    if p == "deep":
        return min(k, 40)
    if p == "balanced":
        return min(k, 18)

    # fast profile
    if _fast_enabled():
        return min(k, 6)
    return min(k, 12)


def _effective_context_chars(context_profile: str) -> int:
    """
    Target context budget for the prompt, BEFORE hard ceiling.
    """
    p = (context_profile or "fast").strip().lower()
    if p == "deep":
        return 60000
    if p == "balanced":
        return 24000
    if _fast_enabled():
        return int((_env("RAG_FAST_CONTEXT_MAX_CHARS", "9000") or "9000").strip() or "9000")
    return 18000


def _effective_snippet_chars(context_profile: str) -> int:
    """
    How much of each chunk to include in the prompt context.
    """
    p = (context_profile or "fast").strip().lower()
    if p == "deep":
        return 1400
    if p == "balanced":
        return 1000
    return 900


# -----------------------------
# Storage + chunking
# -----------------------------
def _read_review_docs(storage: StorageProvider, review_id: str) -> List[Dict[str, Any]]:
    reviews = _read_reviews_file(storage)
    for r in reviews:
        if str(r.get("id")) == str(review_id):
            return list(r.get("docs") or [])
    raise KeyError(f"Review not found: {review_id}")


def _chunk_text(text: str, chunk_size: int, overlap: int) -> List[Tuple[int, int, str]]:
    text = _normalize_text(text)
    n = len(text)
    if n == 0:
        return []
    chunk_size = max(400, int(chunk_size))
    overlap = max(0, min(int(overlap), chunk_size // 2))
    step = max(1, chunk_size - overlap)
    out: List[Tuple[int, int, str]] = []
    i = 0
    while i < n:
        j = min(n, i + chunk_size)
        out.append((i, j, text[i:j]))
        if j == n:
            break
        i += step
    return out


def ingest_review_docs(
    *,
    storage: StorageProvider,
    vector: VectorStore,
    llm: Any,  # LLMProvider
    review_id: str,
) -> Dict[str, Any]:
    docs = _read_review_docs(storage, review_id)

    chunk_size = int((_env("RAG_CHUNK_SIZE", "1400") or "1400").strip() or "1400")
    overlap = int((_env("RAG_CHUNK_OVERLAP", "180") or "180").strip() or "180")

    total_chunks = 0
    doc_results: List[Dict[str, Any]] = []

    for d in docs:
        doc_id = str(d.get("id") or "").strip()
        doc_name = str(d.get("name") or doc_id or "UnknownDoc").strip()
        content = str(d.get("content") or "").strip()

        if not doc_id or not content:
            doc_results.append({"doc_id": doc_id or None, "doc_name": doc_name, "chunks": 0, "skipped": True})
            continue

        chunks = _chunk_text(content, chunk_size=chunk_size, overlap=overlap)
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
    llm: Any,
    question: str,
    top_k: int = 12,
    filters: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    q_emb = llm.embed_texts([question])[0]
    hits = vector.query(query_embedding=q_emb, top_k=int(top_k), filters=filters or {})

    seen: set[str] = set()
    out: List[Dict[str, Any]] = []
    for h in hits or []:
        meta = h.get("meta") or {}
        cid = str(h.get("chunk_id") or "")
        if not cid:
            cid = f"{meta.get('doc_id')}:{meta.get('char_start')}:{meta.get('char_end')}"
        if cid in seen:
            continue
        seen.add(cid)
        out.append(h)
    return out


# -----------------------------
# Questions
# -----------------------------
def _review_summary_questions() -> List[str]:
    return [
        "What is the mission and objective of this effort?",
        "What is the scope of work and required deliverables?",
        "What are the security, compliance, and hosting constraints (IL levels, NIST, DFARS, CUI, ATO/RMF, logging)?",
        "What are the eligibility and personnel constraints (citizenship, clearances, facility, location, export controls)?",
        "What are key legal and data rights risks (IP/data rights, audit rights, flowdowns)?",
        "What are key financial risks (pricing model, ceilings, invoicing systems, payment terms)?",
        "What are submission instructions and deadlines, including required formats and delivery method?",
        "What contradictions or inconsistencies exist across documents?",
        "What gaps require clarification from the Government?",
        "What internal actions should we take next (security/legal/PM/engineering/finance)?",
    ]


def _risk_triage_questions() -> List[str]:
    # Human-in-the-loop triage: focus on likely risk language and obligations.
    return [
        "Identify cybersecurity / ATO / RMF / IL requirements and risks (encryption, logging, incident reporting, vuln mgmt).",
        "Identify CUI handling / safeguarding requirements and risks (marking, access, transmission, storage, disposal).",
        "Identify privacy / PII / data protection obligations and risks.",
        "Identify legal/data-rights terms and risks (IP/data rights, audit rights, GFI/GFM handling, disclosure penalties).",
        "Identify subcontractor / flowdown / staffing constraints and risks (citizenship, clearance, facility, export).",
        "Identify delivery/acceptance gates and required approvals (CDRLs, QA, test, acceptance criteria).",
        "Identify financial and invoicing risks (ceilings, overruns, payment terms, reporting cadence).",
        "Identify schedule risks (IMS, milestones, reporting cadence, penalties).",
        "Identify ambiguous/undefined terms and contradictions that require clarification.",
        "List top red-flag phrases/requirements with evidence and suggested internal owner (security/legal/PM/finance).",
    ]


# -----------------------------
# Sections parsing for UI
# -----------------------------
def _slug(s: str) -> str:
    s = (s or "").strip().lower()
    out: List[str] = []
    for ch in s:
        if ch.isalnum():
            out.append(ch)
        elif ch in (" ", "-", "_", "/"):
            out.append("-")
    slug = "".join(out)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-") or "section"


def _parse_review_summary_sections(text: str) -> List[Dict[str, Any]]:
    raw = (text or "").replace("\r\n", "\n")
    lines = raw.split("\n")

    header_to_idx: Dict[str, int] = {}
    for i, ln in enumerate(lines):
        t = (ln or "").strip()
        if t in RAG_REVIEW_SUMMARY_SECTIONS:
            header_to_idx[t] = i

    headers = [h for h in RAG_REVIEW_SUMMARY_SECTIONS if h in header_to_idx]

    sections: List[Dict[str, Any]] = []
    for j, h in enumerate(headers):
        start = header_to_idx[h]
        end = header_to_idx[headers[j + 1]] if j + 1 < len(headers) else len(lines)

        block_lines = [x.rstrip() for x in lines[start + 1 : end]]
        block = "\n".join(block_lines).strip()

        sec: Dict[str, Any] = {
            "id": _slug(h),
            "title": h,
            "findings": [],
            "evidence": [],
            "gaps": [],
            "recommended_actions": [],
        }

        if not block:
            sections.append(sec)
            continue

        if INSUFFICIENT in block:
            sec["gaps"].append(INSUFFICIENT)
            sections.append(sec)
            continue

        mode: Optional[str] = None  # "findings" | "evidence"
        for ln in block_lines:
            t = (ln or "").strip()
            if not t:
                continue

            if t.lower().startswith("findings"):
                mode = "findings"
                continue
            if t.lower().startswith("evidence"):
                mode = "evidence"
                continue
            if t.lower().startswith("to verify") or t.lower().startswith("to clarify"):
                mode = "gaps"
                continue
            if t.lower().startswith("recommended") or t.lower().startswith("suggested owner"):
                mode = "recommended_actions"
                # keep parsing content lines below as actions
                continue

            is_bullet = t.startswith(("-", "ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¾Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â¦ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢", "*"))
            bullet_text = t.lstrip("-ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¾Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â¦ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢*").strip() if is_bullet else t

            if mode == "findings":
                sec["findings"].append(bullet_text)
            elif mode == "evidence":
                # Evidence is attached deterministically from retrieval/citations.
                continue
            elif mode == "gaps":
                sec["gaps"].append(bullet_text)
            elif mode == "recommended_actions":
                sec["recommended_actions"].append(bullet_text)
            else:
                sec["findings"].append(bullet_text)

        sections.append(sec)

    return sections


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        return default if v is None or str(v).strip() == "" else int(str(v).strip())
    except Exception:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "y", "on")


_GLOSSARY_RE = re.compile(r"\b(glossary|definitions?|for purposes of|means)\b", re.IGNORECASE)
_SIGNAL_RE = re.compile(r"\b(shall|must|required|will|may not|prohibited)\b", re.IGNORECASE)
_COMPLIANCE_RE = re.compile(
    r"\b(dfars|far|nist|cui|cdi|rmf|ato|il[0-9]|fedramp|800-53|800-171|incident|breach|encryption|audit|logging|sbom|zero trust)\b",
    re.IGNORECASE,
)


def _is_glossary_text(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    # strong cues
    if "GLOSSARY" in t.upper() or "DEFINITIONS" in t.upper():
        return True
    return bool(_GLOSSARY_RE.search(t))


def _evidence_signal_score(text: str) -> int:
    """
    Deterministic heuristic score: higher = more likely to be obligations/risk language.
    """
    t = (text or "").strip()
    if not t:
        return 0

    score = 0

    # obligation language
    if _SIGNAL_RE.search(t):
        score += 3

    # compliance keywords
    if _COMPLIANCE_RE.search(t):
        score += 2

    # penalize glossary/definition-heavy chunks
    if _is_glossary_text(t):
        score -= 3

    return score

def _attach_evidence_to_sections(
    sections: List[Dict[str, Any]],
    questions: List[str],
    citations: List[Dict[str, Any]],
    retrieved: Dict[str, List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """
    Attach evidence to parsed sections deterministically, but prefer high-signal chunks
    and avoid glossary/definitions noise by default.
    """
    # knobs
    max_per_section = _env_int("RAG_EVIDENCE_MAX_PER_SECTION", 3)
    allow_glossary = _env_bool("RAG_EVIDENCE_ALLOW_GLOSSARY", False)
    min_signal = _env_int("RAG_EVIDENCE_MIN_SIGNAL", 1)

    # Map question index -> section title (base UI sections)
    # NOTE: keep existing mapping behavior but improve evidence selection quality.
    question_to_section_title: Dict[int, str] = {
        0: "OVERVIEW",  # keep this behavior for triage
    }

    # Normalize section dicts
    for sec in sections:
        sec.setdefault("evidence", [])
        sec.setdefault("findings", [])
        sec.setdefault("gaps", [])
        sec.setdefault("recommended_actions", [])
        sec.setdefault("_evidence_seen", set())

    sec_by_title = {(s.get("title") or "").strip(): s for s in sections}
    # Defensive: normalize citations/retrieved types (prevents list.get crashes)
    if not isinstance(citations, list):
        citations = []
    if not isinstance(retrieved, dict):
        retrieved = {}

    def add_ev(sec: Dict[str, Any], ev: Dict[str, Any]) -> None:
        seen = sec.setdefault("_evidence_seen", set())
        key = f'{ev.get("docId")}:{ev.get("charStart")}:{ev.get("charEnd")}'
        if key in seen:
            return
        seen.add(key)
        sec["evidence"].append(ev)

    def rank_hits(hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        def sort_key(h: Dict[str, Any]) -> Any:
            text = h.get("chunk_text") or h.get("snippet") or ""
            sig = _evidence_signal_score(text)
            vs = h.get("score")
            try:
                vsf = float(vs) if vs is not None else 0.0
            except Exception:
                vsf = 0.0
            # Higher signal first, then higher vector score
            return (-sig, -vsf)
        return sorted(hits or [], key=sort_key)

    def accept_text(text: str) -> bool:
        if not text:
            return False
        sig = _evidence_signal_score(text)
        if (not allow_glossary) and _is_glossary_text(text):
            return False
        if sig < min_signal:
            return False
        return True

    # 1) Prefer retrieved hit chunks (ranked + filtered)
    for i, q in enumerate(questions):
        sec_title = question_to_section_title.get(i)
        if not sec_title:
            # Try to use section by index if question list aligns with section order
            if i < len(sections):
                sec_title = (sections[i].get("title") or "").strip()
        if not sec_title:
            continue

        sec = sec_by_title.get(sec_title)
        if not sec:
            continue

        hits = rank_hits(retrieved.get(q) or [])
        kept = 0

        for h in hits:
            text = (h.get("chunk_text") or "").strip()
            # skip glossary evidence outside overview
            if _is_glossary_text(text) and (sec.get("id") or "").lower() != "overview":
                continue

            if not accept_text(text):
                continue

            ev = {
                "docId": h.get("document_id"),
                "doc": h.get("doc_name"),
                "text": text[:1000],
                "charStart": h.get("char_start"),
                "charEnd": h.get("char_end"),
                "score": h.get("score"),
            }
            add_ev(sec, ev)
            kept += 1
            if kept >= max_per_section:
                break

    # 2) Fallback from citations when a section has no evidence (also filtered)
    for sec in sections:
        if sec.get("evidence"):
            continue

        title = (sec.get("title") or "").strip()
        if not title:
            continue

        # choose citations whose question matches the section index (best-effort)
        # keep existing behavior: take first 3 matching question citations
        idx = None
        for j, s in enumerate(sections):
            if (s.get("title") or "").strip() == title:
                idx = j
                break
        if idx is None or idx >= len(questions):
            continue

        q = questions[idx]
        for c in [x for x in citations if x.get("question") == q]:
            text = (c.get("snippet") or "").strip()
            if not accept_text(text):
                continue

            ev = {
                "docId": c.get("docId"),
                "doc": c.get("doc"),
                "text": text[:1000],
                "charStart": c.get("charStart"),
                "charEnd": c.get("charEnd"),
                "score": c.get("score"),
            }
            add_ev(sec, ev)
            if len(sec["evidence"]) >= max_per_section:
                break

    # Cleanup temp fields
    for s in sections:
        if "_evidence_seen" in s:
            del s["_evidence_seen"]

    return sections

def _section_keywords(section_id: str) -> List[str]:
    sid = (section_id or "").strip().lower()
    m: Dict[str, List[str]] = {
        "overview": ["shall", "must", "required", "prohibited", "rmf", "ato", "il", "nist", "dfars", "deliverables", "cdrl"],
        "mission-objective": ["mission", "objective", "goal", "purpose", "intent"],
        "scope-of-work": ["scope", "work", "tasks", "responsible", "shall", "provide", "perform", "support"],
        "deliverables-timelines": ["deliverable", "cdrl", "submission", "due", "weekly", "monthly", "quarterly", "schedule", "ims", "timeline", "approval"],
        "security-compliance-hosting-constraints": ["rmf", "ato", "il", "impact level", "nist", "800-53", "800-171", "encryption", "logging", "audit", "conmon", "incident", "breach", "zero trust"],
        "eligibility-personnel-constraints": ["citizenship", "clearance", "personnel", "staffing", "subcontractor", "flow down", "export", "fsi", "vet"],
        "legal-data-rights-risks": ["data rights", "ip", "license", "audit rights", "gfi", "gfm", "disclosure", "penalty", "confidential", "rights"],
        "financial-risks": ["cost", "ceiling", "invoice", "payment", "burn rate", "overrun", "funding", "price", "rom"],
        "submission-instructions-deadlines": ["submission", "deadline", "instructions", "proposal", "format", "due"],
        "contradictions-inconsistencies": ["conflict", "inconsistent", "contradiction", "ambiguous", "undefined", "clarify"],
        "gaps-questions-for-the-government": ["clarify", "confirm", "government", "missing", "undefined", "gap"],
        "recommended-internal-actions": ["recommend", "action", "internal", "confirm", "assign", "owner", "review"],
    }
    return m.get(sid, [])


def _text_matches_keywords(text: str, keywords: List[str]) -> bool:
    t = (text or "").lower()
    for k in (keywords or []):
        if k and k.lower() in t:
            return True
    return False


def _format_evidence_bullet(prefix: str, ev: Dict[str, Any]) -> str:
    doc = ev.get("doc") or ev.get("docId") or "UnknownDoc"
    cs = ev.get("charStart")
    ce = ev.get("charEnd")
    snippet = (ev.get("text") or "").strip().replace("\r", " ").replace("\n", " ")
    snippet = snippet[:220]
    return f"{prefix}: {snippet} (Doc: {doc} span: {cs}-{ce})"


def _owner_for_section(section_id: str) -> str:
    sid = (section_id or "").strip().lower()
    m = {
        "overview": "Program/PM",
        "mission-objective": "Program/PM",
        "scope-of-work": "Program/PM",
        "deliverables-timelines": "Program/PM",
        "security-compliance-hosting-constraints": "Security/ISSO",
        "eligibility-personnel-constraints": "Program/PM",
        "legal-data-rights-risks": "Legal/Contracts",
        "financial-risks": "Finance",
        "submission-instructions-deadlines": "Program/PM",
        "contradictions-inconsistencies": "Legal/Contracts",
        "gaps-questions-for-the-government": "Program/PM",
        "recommended-internal-actions": "Program/PM",
    }
    return m.get(sid, "Program/PM")

def _risk_blurb_for_section(section_id: str, ev_text: str) -> str:
    sid = (section_id or "").strip().lower()
    t = (ev_text or "").strip()

    # keep it deterministic and non-hallucinating: cite the observed obligation phrase (short) + what to confirm
    def short(s: str, n: int = 160) -> str:
        s = (s or "").replace("\r"," ").replace("\n"," ").strip()
        return s if len(s) <= n else (s[:n] + "...")

    if sid == "security-compliance-hosting-constraints":
        return "Obligation/security constraint detected. Confirm IL level, RMF/ATO responsibility split, CONMON/log retention, and any prohibited actions. Evidence: " + short(t)
    if sid == "legal-data-rights-risks":
        return "Possible legal/data-rights obligation detected. Confirm IP/data rights, disclosure penalties, audit rights, and flowdown requirements. Evidence: " + short(t)
    if sid == "financial-risks":
        return "Possible cost/reporting obligation detected. Confirm burn-rate reporting, overruns notification, ceilings, and invoicing cadence. Evidence: " + short(t)
    if sid == "deliverables-timelines":
        return "Deliverable/acceptance obligation language detected. Confirm CDRLs, due dates, acceptance criteria, and Government approval gates. Evidence: " + short(t)
    if sid == "eligibility-personnel-constraints":
        return "Eligibility/personnel constraint language detected. Confirm citizenship/clearance requirements, subcontractor restrictions, and access constraints. Evidence: " + short(t)
    if sid == "submission-instructions-deadlines":
        return "Submission/instruction obligation language detected. Confirm format, due dates, delivery mechanism, and required attachments. Evidence: " + short(t)
    if sid == "contradictions-inconsistencies":
        return "Potential inconsistency/ambiguity risk. Confirm conflicting requirements, undefined terms, or updates to applicable documents list. Evidence: " + short(t)
    if sid == "scope-of-work":
        return "Scope/task obligation language detected. Confirm who does what, required tasks, and dependencies/assumptions. Evidence: " + short(t)
    if sid == "mission-objective":
        return "Mission/objective may be unclear or missing in retrieved content. Confirm purpose/goal language and success criteria from the contract/solicitation. Evidence: " + short(t)

    return "Obligation language detected. Confirm scope, acceptance gates, and Gov vs Contractor responsibility. Evidence: " + short(t)

def _backfill_sections_from_evidence(
    sections: List[Dict[str, Any]],
    intent: str,
) -> List[Dict[str, Any]]:
    """
    Deterministically ensure each section is populated:
    - If findings are empty, convert relevant evidence into EVIDENCE bullets.
    - If evidence is empty, add clear GAPS bullets (no hallucination).
    - For risk_triage only: allow POTENTIAL RISK bullets when evidence exists but implication needs confirmation.
    """
    intent = (intent or "strict_summary").strip().lower()
    is_triage = intent == "risk_triage"

    for sec in sections:
        sid = (sec.get("id") or "").strip().lower()
        title = (sec.get("title") or "").strip()
        findings = sec.get("findings") or []
        gaps = sec.get("gaps") or []
        actions = sec.get("recommended_actions") or []
        evidence = sec.get("evidence") or []

        # Normalize arrays
        sec["findings"] = list(findings)
        sec["gaps"] = list(gaps)
        sec["recommended_actions"] = list(actions)
        sec["evidence"] = list(evidence)
        # If evidence exists, do not keep the generic "Insufficient evidence retrieved" gap
        if sec["evidence"]:
            sec["gaps"] = [g for g in sec["gaps"] if "Insufficient evidence retrieved" not in str(g)]


        kw = _section_keywords(sid)

        # If we have evidence but no findings, synthesize findings from evidence
        if sec["evidence"] and not sec["findings"]:
            kept = 0
            for ev in sec["evidence"]:
                if not _text_matches_keywords(ev.get("text") or "", kw) and sid != "overview":
                    continue
                sec["findings"].append(_format_evidence_bullet("EVIDENCE", ev))
                kept += 1
                if kept >= 3:
                    break

            # If still empty (evidence not topical), add a conservative note
            if not sec["findings"] and sid != "overview":
                if not sec["findings"]:
                    sec["findings"].append("GAP: Evidence retrieved appears non-topical for this section (likely glossary/definitions). Mission/objective language may be elsewhere; request/ingest the contract section defining purpose/mission and rerun triage.")
                sec["gaps"].append("Retrieved evidence appears non-topical (likely definitions/glossary) for this section. Recommend targeted retrieval for this section and rerun analysis.")

        # If triage and we have evidence, add 1 potential risk when warranted
        if is_triage and sec["evidence"]:
            # very conservative: only add potential risk if we have obligation keywords
            ev0 = (sec["evidence"][0].get("text") or "").lower()
            if any(x in ev0 for x in ["shall", "must", "required", "prohibited"]) and sid not in ("overview",):
                sec["findings"].append(f"POTENTIAL RISK (Owner: {_owner_for_section(sid)}): " + _risk_blurb_for_section(sid, sec["evidence"][0].get("text") if sec["evidence"] else ""))

        # If nothing at all, add a gap (never fabricate)
        if (not sec["findings"]) and (not sec["evidence"]):
            if not sec["gaps"]:
                sec["gaps"].append("Insufficient evidence retrieved for this section. Confirm relevant contract sections and rerun analysis.")
            if not sec["recommended_actions"]:
                sec["recommended_actions"].append("Request the relevant section(s) from the Government/PM and rerun RAG triage.")

        # Small per-section action hints (deterministic)
        if sid == "security-compliance-hosting-constraints" and sec["evidence"] and not sec["recommended_actions"]:
            sec["recommended_actions"].append("Confirm IL level, ATO boundary responsibilities (Gov vs Contractor), and required RMF artifacts/acceptance gates.")
        if sid == "deliverables-timelines" and sec["evidence"] and not sec["recommended_actions"]:
            sec["recommended_actions"].append("Extract deliverables/approval gates into a tracker (CDRLs, cadence, acceptance criteria) and confirm IMS requirements.")
        if sid == "eligibility-personnel-constraints" and sec["evidence"] and not sec["recommended_actions"]:
            sec["recommended_actions"].append("Confirm staffing/citizenship/clearance/flowdown constraints and ensure subcontractor vetting language is feasible.")

    return sections


def _strengthen_overview_from_evidence(sections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Ensure OVERVIEW is always strong:
    - Pull top evidence bullets across all sections.
    - Summarize obligations/constraints in deterministic bullets.
    """
    ov = None
    for s in sections:
        if (s.get("id") or "").strip().lower() == "overview":
            ov = s
            break
    if ov is None:
        return sections

    # Collect high-signal evidence from all sections
    pool: List[Dict[str, Any]] = []
    for s in sections:
        for ev in (s.get("evidence") or []):
            pool.append(ev)

    def score_ev(ev: Dict[str, Any]) -> Any:
        t = (ev.get("text") or "")
        sig = _evidence_signal_score(t)
        vs = ev.get("score")
        try:
            vsf = float(vs) if vs is not None else 0.0
        except Exception:
            vsf = 0.0
        return (-sig, -vsf)

    pool = sorted(pool, key=score_ev)

    # Add deterministic overview bullets if missing depth
    ov.setdefault("findings", [])
    existing = ov.get("findings") or []
    ov["findings"] = list(existing)
    # Always ensure at least 3 evidence-labeled bullets at the top of OVERVIEW findings
    if not any(str(x).startswith("EVIDENCE:") for x in ov["findings"]):
        injected: List[str] = []
        added = 0
        for ev in pool:
            if _is_glossary_text(ev.get("text") or ""):
                continue
            injected.append(_format_evidence_bullet("EVIDENCE", ev))
            added += 1
            if added >= 3:
                break
        ov["findings"] = injected + ov["findings"]


    if len(ov["findings"]) < 6:
        added = 0
        for ev in pool:
            if _is_glossary_text(ev.get("text") or ""):
                continue
            ov["findings"].append(_format_evidence_bullet("EVIDENCE", ev))
            added += 1
            if added >= 6:
                break

    if not ov.get("recommended_actions"):
        ov["recommended_actions"] = [
            "Review top obligations/constraints surfaced below and assign owners (Security/ISSO, PM, Engineering, Legal, Finance) for confirmation and response planning."
        ]

    return sections
# -----------------------------
# Main entry
# -----------------------------
def rag_analyze_review(
    *,
    storage: StorageProvider,
    vector: VectorStore,
    llm: Any,
    review_id: str,
    top_k: int = 12,
    force_reingest: bool = False,
    mode: Optional[str] = None,
    analysis_intent: str = "strict_summary",
    context_profile: str = "fast",
    debug: bool = False,
) -> Dict[str, Any]:
    t0 = time.time() if _timing_enabled() else 0.0
    m = _canonical_mode(mode)

    # DEV guardrail: avoid expensive re-ingest loops during fast mode unless explicitly allowed.
    if _fast_enabled() and force_reingest and (_env("RAG_ALLOW_FORCE_REINGEST", "0").strip() != "1"):
        print("[RAG] WARN: force_reingest requested but skipped because RAG_FAST=1 and RAG_ALLOW_FORCE_REINGEST!=1")
        force_reingest = False

    intent = (analysis_intent or "strict_summary").strip().lower()
    profile = (context_profile or "fast").strip().lower()

    if _timing_enabled():
        print("[RAG] analyze start", review_id, f"mode={m} intent={intent} profile={profile}")

    if force_reingest:
        t_ing0 = time.time() if _timing_enabled() else 0.0
        ingest_review_docs(storage=storage, vector=vector, llm=llm, review_id=review_id)
        if _timing_enabled():
            print("[RAG] ingest done", round(time.time() - t_ing0, 2), "s")

    if intent == "risk_triage":
        questions = _risk_triage_questions()
    else:
        questions = _review_summary_questions()

    effective_top_k = _effective_top_k(top_k, profile)
    retrieved: Dict[str, List[Dict[str, Any]]] = {}
    citations: List[Dict[str, Any]] = []

    t_ret0 = time.time() if _timing_enabled() else 0.0
    for q in questions:
        retrieved[q] = query_review(
            vector=vector,
            llm=llm,
            question=q,
            top_k=effective_top_k,
            filters={"review_id": review_id},
        )
    if _timing_enabled():
        print("[RAG] retrieval done", round(time.time() - t_ret0, 2), "s")

    # Build prompt context (retrieved evidence only)
    snippet_cap = _effective_snippet_chars(profile)

    def fmt_hit(h: Dict[str, Any]) -> str:
        meta = h.get("meta") or {}
        doc = meta.get("doc_name") or h.get("doc_name") or meta.get("doc_id") or "UnknownDoc"
        cs = meta.get("char_start")
        ce = meta.get("char_end")
        score = h.get("score")
        chunk_text = (h.get("chunk_text") or "").strip()
        snippet = chunk_text[:snippet_cap]
        return (
            "===BEGIN CONTRACT EVIDENCE===\n"
            f"DOC: {doc} | score={score} | span={cs}-{ce}\n"
            f"{snippet}\n"
            "===END CONTRACT EVIDENCE==="
        )

    blocks: List[str] = []
    for q in questions:
        hits = retrieved.get(q) or []
        blocks.append(
            f"QUESTION: {q}\nRETRIEVED EVIDENCE:\n"
            + "\n".join(fmt_hit(h) for h in hits[:effective_top_k])
        )

    context = "\n\n".join(blocks)

    # -----------------------------
    # Context cap: make triage able to expand beyond 16k
    # -----------------------------
    # Baseline env cap (legacy behavior)
    env_cap = int((_env("RAG_CONTEXT_MAX_CHARS", "16000") or "16000").strip() or "16000")
    # Hard ceiling safety valve
    hard_cap = int((_env("RAG_HARD_CONTEXT_MAX_CHARS", "80000") or "80000").strip() or "80000")
    # Profile target cap
    profile_cap = int(_effective_context_chars(profile))

    if intent == "risk_triage":
        # allow deep/balanced to override env baseline, but never exceed hard cap
        max_chars = min(max(env_cap, profile_cap), hard_cap)
    else:
        # strict_summary remains conservative and respects env baseline
        max_chars = min(env_cap, profile_cap)

    context = context[:max_chars]


    # -----------------------------
    # Prompt
    # -----------------------------
    if intent == "risk_triage":
        prompt = (
            "OVERVIEW\n"
            "You are performing HUMAN-IN-THE-LOOP RISK TRIAGE on government/DoD contract documents.\n"
            "Goal: quickly surface likely risks, obligations, and red flags for a reviewer to confirm.\n\n"
            "HARD RULES\n"
            "- Plain text only. No markdown.\n"
            "- Do NOT fabricate requirements. If you cannot find evidence, write exactly: "
            f"\"{INSUFFICIENT}\"\n"
            "- Prefer specificity: name the obligation and why it is risky.\n"
            "- For each section: include Findings (bullets) + Evidence (1-3 snippets copied from evidence blocks).\n"
            "- If evidence is weak/implicit, label it as 'Potential risk' and explain what to confirm.\n"
            "- Suggested owner must be one of: Security/ISSO, Legal/Contracts, Program/PM, Engineering, Finance, QA.\n\n"
            "SECTIONS (exact order)\n"
            + "\n".join(RAG_REVIEW_SUMMARY_SECTIONS)
            + "\n\n"
            "RETRIEVED CONTEXT\n"
            f"{context}\n"
        ).strip()
    else:
        prompt = (
            "OVERVIEW\n"
            "Write ONE unified cross-document executive brief for this review.\n\n"
            "HARD RULES\n"
            "- Plain text only. No markdown.\n"
            "- Do NOT output bracket placeholders like \"[insert ...]\" or any templating filler.\n"
            f"- If you cannot find evidence for a section, write exactly: \"{INSUFFICIENT}\"\n"
            "- Do not fabricate deliverables, dates, roles, responsibilities, or requirements.\n"
            "- You MUST use evidence from the retrieved context only.\n"
            "- Evidence MUST be copied only from within blocks between:\n"
            "  ===BEGIN CONTRACT EVIDENCE=== and ===END CONTRACT EVIDENCE===\n"
            "- Do NOT treat the QUESTION lines, headings, or instructions as evidence.\n\n"
            "FORMAT RULES\n"
            "- Use the SECTION HEADERS exactly as listed below, in the exact order.\n"
            "- For EACH SECTION:\n"
            "  1) Findings (bullets)\n"
            "  2) Evidence: 1-3 short snippets copied EXACTLY from retrieved context\n"
            "  3) If insufficient evidence, state what to retrieve/clarify\n\n"
            "SECTIONS (exact order)\n"
            + "\n".join(RAG_REVIEW_SUMMARY_SECTIONS)
            + "\n\n"
            "RETRIEVED CONTEXT\n"
            f"{context}\n"
        ).strip()

    t_gen0 = time.time() if _timing_enabled() else 0.0
    if _timing_enabled():
        print("[RAG] generation start")

    summary_raw = (llm.generate(prompt) or {}).get("text") or ""
    summary = _postprocess_review_summary(summary_raw)

    # citations: stable
    for q in questions:
        for h in (retrieved.get(q) or [])[: min(3, int(effective_top_k))]:
            meta = h.get("meta") or {}
            citations.append(
                {
                    "question": q,
                    "doc": meta.get("doc_name") or meta.get("doc_id"),
                    "docId": meta.get("doc_id"),
                    "charStart": meta.get("char_start"),
                    "charEnd": meta.get("char_end"),
                    "score": h.get("score"),
                    "snippet": ((h.get("chunk_text") or "").strip()[:350] or None),
                }
            )

    parsed_sections = _attach_evidence_to_sections(
        sections=_parse_review_summary_sections(summary),
        questions=questions,
        citations=citations,
        retrieved=retrieved,
    )
    parsed_sections = _backfill_sections_from_evidence(parsed_sections, intent)
    parsed_sections = _strengthen_overview_from_evidence(parsed_sections)


    if _timing_enabled():
        print("[RAG] generation done", round(time.time() - t_gen0, 2), "s")
        print("[RAG] analyze done", round(time.time() - t0, 2), "s")

    # --- stats / warnings ---
    retrieved_total = sum(len(retrieved.get(q) or []) for q in questions)
    warnings: List[str] = []

    zero_hit_questions = [q for q in questions if len(retrieved.get(q) or []) == 0]
    if zero_hit_questions:
        warnings.append(f"Insufficient evidence for {len(zero_hit_questions)} section(s).")

    context_used_chars = len(context)
    context_truncated = bool(context_used_chars >= max_chars)
    if context_truncated:
        warnings.append(f"Context truncated at {max_chars} chars.")

    # Optional debug payload
    retrieved_debug = None
    if debug:
        retrieved_debug = {}
        limit = min(3, int(effective_top_k))
        for q in questions:
            hits = (retrieved.get(q) or [])[:limit]
            out_hits = []
            for h in hits:
                meta = h.get("meta") or {}
                out_hits.append(
                    {
                        "docId": meta.get("doc_id"),
                        "doc": meta.get("doc_name") or meta.get("doc_id"),
                        "score": h.get("score"),
                        "charStart": meta.get("char_start"),
                        "charEnd": meta.get("char_end"),
                        "snippet": ((h.get("chunk_text") or "").strip()[:350] or None),
                    }
                )
            retrieved_debug[q] = out_hits

    return {
        "review_id": review_id,
        "mode": m,
        "top_k": int(top_k),
        "analysis_intent": intent,
        "context_profile": profile,
        "summary": summary,
        "citations": citations,
        "retrieved_counts": {q: len(retrieved.get(q) or []) for q in questions},
        "sections": parsed_sections,
        "stats": {
            "top_k_effective": int(effective_top_k),
            "analysis_intent": str(intent),
            "context_profile": str(profile),
            "retrieved_total": int(retrieved_total),
            "context_max_chars": int(max_chars),
            "context_used_chars": int(context_used_chars),
            "context_truncated": bool(context_truncated),
            "fast_mode": bool(_fast_enabled()),
        },
        "warnings": warnings,
        "retrieved": retrieved_debug,
    }




