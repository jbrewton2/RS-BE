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

            is_bullet = t.startswith(("-", "•", "*"))
            bullet_text = t.lstrip("-•*").strip() if is_bullet else t

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


def _attach_evidence_to_sections(
    sections: List[Dict[str, Any]],
    questions: List[str],
    retrieved: Dict[str, List[Dict[str, Any]]],
    citations: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    sec_by_title = {(s.get("title") or "").strip(): s for s in sections}

    # Map question index -> section title (base UI sections)
    q_to_title = {
        0: "OVERVIEW",  # map first triage bucket to overview too
        1: "SCOPE OF WORK",
        2: "SECURITY, COMPLIANCE & HOSTING CONSTRAINTS",
        3: "ELIGIBILITY & PERSONNEL CONSTRAINTS",
        4: "LEGAL & DATA RIGHTS RISKS",
        5: "DELIVERABLES & TIMELINES",
        6: "FINANCIAL RISKS",
        7: "DELIVERABLES & TIMELINES",
        8: "CONTRADICTIONS & INCONSISTENCIES",
        9: "RECOMMENDED INTERNAL ACTIONS",
    }

    def add_ev(sec: Dict[str, Any], ev: Dict[str, Any]) -> None:
        sec.setdefault("evidence", [])
        key = f'{ev.get("docId")}:{ev.get("charStart")}:{ev.get("charEnd")}:{(ev.get("text") or "")[:40]}'
        seen = sec.setdefault("_evidence_seen", set())
        if key in seen:
            return
        seen.add(key)
        sec["evidence"].append(ev)

    # Prefer retrieved hit chunks
    for i, q in enumerate(questions):
        title = q_to_title.get(i)
        if not title:
            continue
        sec = sec_by_title.get(title)
        if not sec:
            continue

        hits = retrieved.get(q) or []
        for h in hits[:3]:
            meta = h.get("meta") or {}
            doc_id = meta.get("doc_id")
            doc_name = meta.get("doc_name") or doc_id
            text = ((h.get("chunk_text") or "").strip()[:350] or None)
            if not text:
                continue
            add_ev(
                sec,
                {
                    "docId": doc_id or "",
                    "doc": doc_name,
                    "text": text,
                    "charStart": meta.get("char_start"),
                    "charEnd": meta.get("char_end"),
                    "score": h.get("score"),
                },
            )

    # Fallback from citations when a section has no evidence
    for i, q in enumerate(questions):
        title = q_to_title.get(i)
        if not title:
            continue
        sec = sec_by_title.get(title)
        if not sec:
            continue
        if sec.get("evidence"):
            continue

        for c in [x for x in citations if x.get("question") == q][:3]:
            snip = (c.get("snippet") or "").strip()
            if not snip:
                continue
            add_ev(
                sec,
                {
                    "docId": c.get("docId") or "",
                    "doc": c.get("doc") or None,
                    "text": snip,
                    "charStart": c.get("charStart"),
                    "charEnd": c.get("charEnd"),
                    "score": c.get("score"),
                },
            )

    for s in sections:
        if "_evidence_seen" in s:
            del s["_evidence_seen"]

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
        _parse_review_summary_sections(summary),
        questions,
        retrieved,
        citations,
    )

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
