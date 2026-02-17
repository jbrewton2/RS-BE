# rag/service.py
from __future__ import annotations


# -----------------------------
import os
import re

# -----------------------------
# -----------------------------
# SECTION OUTPUT NORMALIZATION
# - Keep evidence in section.evidence[]
# - Keep findings[] as short bullets only
# -----------------------------

# Tuning knobs (UI ergonomics)
_SECTION_MAX_FINDINGS = 6          # clamp findings bullets per section (tight default)
_SECTION_MAX_EVIDENCE = 6          # clamp evidence snippets per section (tight default)
_FINDING_MAX_LEN = 160             # clamp bullet length (tight default)
_DROP_TRAILING_PERIODS = True      # normalize bullets by removing trailing periods

_EVIDENCE_LINE_RE = re.compile(
    r'^\s*EVIDENCE:\s*(?P<snippet>.+?)\s*\(Doc:\s*(?P<doc>.+?)\s*span:\s*(?P<cs>\d+)\-(?P<ce>\d+)\)\s*$',
    re.IGNORECASE,
)

def _extract_evidence_from_finding_line(line: str) -> Optional[Dict[str, Any]]:
    """
    Parse our own formatted evidence line:
      EVIDENCE: <snippet> (Doc: <doc> span: <cs>-<ce>)
    Returns a RagEvidenceSnippet-like dict, or None.
    """
    m = _EVIDENCE_LINE_RE.match(line or "")
    if not m:
        return None
    snippet = (m.group("snippet") or "").strip()
    doc = (m.group("doc") or "").strip()
    try:
        cs = int(m.group("cs"))
        ce = int(m.group("ce"))
    except Exception:
        cs, ce = None, None

    return {
        # NOTE: this format only has doc name, so docId is best-effort.
        "docId": doc,
        "doc": doc,
        "text": snippet,
        "charStart": cs,
        "charEnd": ce,
        "score": None,
    }

def _evidence_key(ev: Dict[str, Any]) -> str:
    """
    Dedupe key for evidence:
      prefer (docId,charStart,charEnd); fall back to (doc,text prefix)
    """
    if not isinstance(ev, dict):
        return ""
    doc_id = str(ev.get("docId") or ev.get("doc") or "").strip()
    cs = ev.get("charStart")
    ce = ev.get("charEnd")
    if doc_id and isinstance(cs, int) and isinstance(ce, int):
        return f"{doc_id}|{cs}|{ce}"
    # fallback: doc + first 80 chars of text
    doc = str(ev.get("doc") or doc_id or "").strip()
    text = str(ev.get("text") or "").strip().replace("\n", " ")
    return f"{doc}|{text[:80]}"

def _normalize_bullet_text(t: str) -> str:
    """
    Deterministic bullet normalization (NO LLM):
      - strip markdown
      - normalize common "Review ..." starters into verb-first
      - optionally remove trailing periods
      - clamp length
    """
    s = (t or "").strip()
    if not s:
        return s

    # Strip markdown decoration
    s = s.replace("**", "").strip()
    sl = s.lower()

    # Verb-first normalization (reduce repetitive "Review ...")
    if sl.startswith("review and ensure "):
        s = "Ensure " + s[len("review and ensure "):].lstrip()
    elif sl.startswith("review and verify "):
        s = "Verify " + s[len("review and verify "):].lstrip()
    elif sl.startswith("review and assess "):
        s = "Assess " + s[len("review and assess "):].lstrip()
    elif sl.startswith("review and understand "):
        s = "Understand " + s[len("review and understand "):].lstrip()
    elif sl.startswith("review to ensure "):
        s = "Ensure " + s[len("review to ensure "):].lstrip()
    elif sl.startswith("review to verify "):
        s = "Verify " + s[len("review to verify "):].lstrip()

    # Normalize "X that ..." -> "X ..." (shorter)
    sl = s.lower()
    if sl.startswith("verify that "):
        s = "Verify " + s[len("verify that "):].lstrip()
    elif sl.startswith("ensure that "):
        s = "Ensure " + s[len("ensure that "):].lstrip()
    elif sl.startswith("confirm that "):
        s = "Confirm " + s[len("confirm that "):].lstrip()

    # Remove trailing periods (UI consistency)
    if _DROP_TRAILING_PERIODS:
        s = s.rstrip()
        while s.endswith("."):
            s = s[:-1].rstrip()

    # Clamp length
    if len(s) > _FINDING_MAX_LEN:
        s = s[:_FINDING_MAX_LEN].rstrip() + "ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦"

    return s

def _clean_findings_line(s: str) -> Optional[str]:
    """
    Findings must be short bullets only (no scaffolding, no evidence blocks).
    """
    t = (s or "").strip()
    if not t:
        return None

    tl = t.lower()
    
    # Drop common LLM intro sentence (broad)
    if tl.startswith("based on the") and ("evidence" in tl) and ("actions" in tl or "next" in tl):
        return None

    # Drop evidence-marker lines (handled into evidence[])
    if _EVIDENCE_LINE_RE.match(t):
        return None

    # Drop obvious evidence/meta artifacts
    if tl.startswith("evidence:") or ("(doc:" in tl and "span:" in tl) or ("charstart" in tl and "charend" in tl):
        return None

    # Drop scaffolding (we want bullets only)
    if tl.startswith("requirement:") or tl.startswith("why it matters:"):
        return None

    # Drop common LLM intro sentence
    if "based on the retrieved evidence" in tl and "actions" in tl:
        return None

    # Drop markdown-ish headers / department headers
    if tl.endswith("**") or tl.endswith(":**"):
        return None
    # e.g. "Security:", "Legal:", "Engineering:", "PM (Program Management):"
    if re.match(r"^[a-z0-9 /()_-]+:\s*$", t, flags=re.IGNORECASE):
        return None

    t = _normalize_bullet_text(t)
    if not t:
        return None

    return t

def _normalize_section_outputs(section: Dict[str, Any], *, max_findings: int = _SECTION_MAX_FINDINGS) -> None:
    """
    Mutates section in place:
      - Moves embedded EVIDENCE lines from findings -> evidence[]
      - Cleans findings into short bullets
      - Dedupes + clamps both evidence and findings
    """
    if not isinstance(section, dict):
        return

    findings_in = section.get("findings") or []
    if not isinstance(findings_in, list):
        findings_in = []

    ev_out = section.get("evidence") or []
    if not isinstance(ev_out, list):
        ev_out = []

    # Move evidence lines out of findings (best-effort)
    for raw in findings_in:
        ev = _extract_evidence_from_finding_line(str(raw or ""))
        if ev:
            ev_out.append(ev)

    # Dedupe + clamp evidence
    ev_seen = set()
    ev_clean: List[Dict[str, Any]] = []
    for ev in ev_out:
        if not isinstance(ev, dict):
            continue
        k = _evidence_key(ev)
        if not k or k in ev_seen:
            continue
        ev_seen.add(k)
        ev_clean.append(ev)
        if len(ev_clean) >= _SECTION_MAX_EVIDENCE:
            break

    # Clean findings
    cleaned: List[str] = []
    seen = set()
    for raw in findings_in:
        t = _clean_findings_line(str(raw or ""))
        if not t:
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(t)
        if len(cleaned) >= max_findings:
            break

    section["evidence"] = ev_clean
    section["findings"] = cleaned

import time
from rag.service_helpers import build_rag_response_dict, materialize_risk_register, retrieve_context
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
# Questions and routing
# -----------------------------
def _question_section_map(intent: str) -> List[Tuple[str, str]]:
    """
    Authoritative routing for evidence attachment.
    Each tuple is (SECTION TITLE, QUESTION).
    """
    intent = (intent or "strict_summary").strip().lower()

    if intent == "risk_triage":
        return [
            (
                "SECURITY, COMPLIANCE & HOSTING CONSTRAINTS",
                "Identify cybersecurity / ATO / RMF / IL requirements and risks (encryption, logging, incident reporting, vuln mgmt).",
            ),
            (
                "SECURITY, COMPLIANCE & HOSTING CONSTRAINTS",
                "Identify CUI handling / safeguarding requirements and risks (marking, access, transmission, storage, disposal).",
            ),
            ("LEGAL & DATA RIGHTS RISKS", "Identify privacy / PII / data protection obligations and risks."),
            (
                "LEGAL & DATA RIGHTS RISKS",
                "Identify legal/data-rights terms and risks (IP/data rights, audit rights, GFI/GFM handling, disclosure penalties).",
            ),
            (
                "ELIGIBILITY & PERSONNEL CONSTRAINTS",
                "Identify subcontractor / flowdown / staffing constraints and risks (citizenship, clearance, facility, export).",
            ),
            ("DELIVERABLES & TIMELINES", "Identify delivery/acceptance gates and required approvals (CDRLs, QA, test, acceptance criteria)."),
            ("FINANCIAL RISKS", "Identify financial and invoicing risks (ceilings, overruns, payment terms, reporting cadence)."),
            ("DELIVERABLES & TIMELINES", "Identify schedule risks (IMS, milestones, reporting cadence, penalties)."),
            ("CONTRADICTIONS & INCONSISTENCIES", "Identify ambiguous/undefined terms and contradictions that require clarification."),
            # overview aggregator
            ("OVERVIEW", "List top red-flag phrases/requirements with evidence and suggested internal owner (security/legal/PM/finance)."),
            ("MISSION & OBJECTIVE", "What is the mission and objective of this effort?"),
            ("SCOPE OF WORK", "What is the scope of work and required deliverables?"),
            ("SUBMISSION INSTRUCTIONS & DEADLINES", "What are submission instructions and deadlines, including required formats and delivery method?"),
            ("GAPS / QUESTIONS FOR THE GOVERNMENT", "What gaps require clarification from the Government?"),
            ("RECOMMENDED INTERNAL ACTIONS", "What internal actions should we take next (security/legal/PM/engineering/finance)?"),
        ]

    # strict_summary
    return [
        ("MISSION & OBJECTIVE", "What is the mission and objective of this effort?"),
        ("SCOPE OF WORK", "What is the scope of work and required deliverables?"),
        ("SECURITY, COMPLIANCE & HOSTING CONSTRAINTS", "What are the security, compliance, and hosting constraints (IL levels, NIST, DFARS, CUI, ATO/RMF, logging)?"),
        ("ELIGIBILITY & PERSONNEL CONSTRAINTS", "What are the eligibility and personnel constraints (citizenship, clearances, facility, location, export controls)?"),
        ("LEGAL & DATA RIGHTS RISKS", "What are key legal and data rights risks (IP/data rights, audit rights, flowdowns)?"),
        ("FINANCIAL RISKS", "What are key financial risks (pricing model, ceilings, invoicing systems, payment terms)?"),
        ("SUBMISSION INSTRUCTIONS & DEADLINES", "What are submission instructions and deadlines, including required formats and delivery method?"),
        ("CONTRADICTIONS & INCONSISTENCIES", "What contradictions or inconsistencies exist across documents?"),
        ("GAPS / QUESTIONS FOR THE GOVERNMENT", "What gaps require clarification from the Government?"),
        ("RECOMMENDED INTERNAL ACTIONS", "What internal actions should we take next (security/legal/PM/engineering/finance)?"),
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
                continue

            is_bullet = t.startswith(("-", "*"))
            bullet_text = t.lstrip("-*").strip() if is_bullet else t

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
    r"\b(dfars|far|nist|cui|cdi|rmf|ato|il[0-9]|fedramp|800-53|800-171|incident|breach|encryption|audit|logging|sbom|zero trust|conmon)\b",
    re.IGNORECASE,
)


def _is_glossary_text(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if "GLOSSARY" in t.upper() or "DEFINITIONS" in t.upper():
        return True
    return bool(_GLOSSARY_RE.search(t))


def _has_obligation_signal(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return bool(_SIGNAL_RE.search(t) or _COMPLIANCE_RE.search(t))


def _evidence_signal_score(text: str) -> int:
    """
    Deterministic heuristic score: higher = more likely to be obligations/risk language.
    """
    t = (text or "").strip()
    if not t:
        return 0

    score = 0
    if _SIGNAL_RE.search(t):
        score += 3
    if _COMPLIANCE_RE.search(t):
        score += 2
    if _is_glossary_text(t):
        score -= 3

    return score


def _extract_obligation_excerpt(text: str, max_len: int = 1200) -> str:
    """
    Make evidence popups more useful:
    take an excerpt around the first obligation/compliance cue, otherwise truncate.
    """
    t = _normalize_text(text or "").strip()
    if not t:
        return ""
    m = _SIGNAL_RE.search(t) or _COMPLIANCE_RE.search(t)
    if not m:
        return t[:max_len]
    start = max(0, m.start() - 250)
    end = min(len(t), start + max_len)
    return t[start:end].strip()


def _attach_evidence_to_sections(
    sections: List[Dict[str, Any]],
    *,
    intent: str,
    section_question_map: List[Tuple[str, str]],
    citations: List[Dict[str, Any]],
    retrieved: Dict[str, List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """
    Attach evidence to parsed sections deterministically, with:
    - explicit section routing (question -> section)
    - glossary suppression unless it contains real obligation signals
    - evidence excerpts that are useful for popups
    """
    max_per_section = _env_int("RAG_EVIDENCE_MAX_PER_SECTION", 3)
    allow_glossary = _env_bool("RAG_EVIDENCE_ALLOW_GLOSSARY", False)
    min_signal = _env_int("RAG_EVIDENCE_MIN_SIGNAL", 1)

    for sec in sections:
        sec.setdefault("evidence", [])
        sec.setdefault("findings", [])
        sec.setdefault("gaps", [])
        sec.setdefault("recommended_actions", [])
        sec.setdefault("_evidence_seen", set())

    sec_by_title = {(s.get("title") or "").strip(): s for s in sections}
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
            return (-sig, -vsf)

        return sorted(hits or [], key=sort_key)

    def accept_text(text: str, sec_id: str) -> bool:
        if not text:
            return False

        sig = _evidence_signal_score(text)

        # Glossary handling:
        # - reject pure glossary unless it also has obligation/compliance signal
        # - allow glossary only when explicitly allowed OR has signal
        if _is_glossary_text(text):
            if not allow_glossary and (not _has_obligation_signal(text)):
                return False
            # outside overview, require at least min_signal if it smells like glossary
            if sec_id != "overview" and sig < min_signal:
                return False

        if sig < min_signal:
            return False
        return True

    # 1) Attach from retrieved (explicit routing)
    for sec_title, q in section_question_map:
        sec = sec_by_title.get(sec_title)
        if not sec:
            continue

        sid = (sec.get("id") or "").strip().lower()
        hits = rank_hits(retrieved.get(q) or [])
        kept = 0

        for h in hits:
            chunk_text = (h.get("chunk_text") or "").strip()
            if not accept_text(chunk_text, sid):
                continue

            meta = h.get("meta") or {}
            ev = {
                "docId": meta.get("doc_id") or h.get("document_id"),
                "doc": meta.get("doc_name") or h.get("doc_name"),
                "text": _extract_obligation_excerpt(chunk_text, max_len=1200),
                "charStart": meta.get("char_start") if meta else h.get("char_start"),
                "charEnd": meta.get("char_end") if meta else h.get("char_end"),
                "score": h.get("score"),
            }
            add_ev(sec, ev)
            kept += 1
            if kept >= max_per_section:
                break

    # 2) Fallback from citations if section has no evidence: use first mapped question for that section
    q_for_section: Dict[str, str] = {}
    for sec_title, q in section_question_map:
        if sec_title not in q_for_section:
            q_for_section[sec_title] = q

    for sec in sections:
        if sec.get("evidence"):
            continue
        title = (sec.get("title") or "").strip()
        if not title:
            continue

        q = q_for_section.get(title)
        if not q:
            continue

        sid = (sec.get("id") or "").strip().lower()
        for c in [x for x in citations if x.get("question") == q]:
            text = (c.get("snippet") or "").strip()
            if not accept_text(text, sid):
                continue

            ev = {
                "docId": c.get("docId"),
                "doc": c.get("doc"),
                "text": _extract_obligation_excerpt(text, max_len=1200),
                "charStart": c.get("charStart"),
                "charEnd": c.get("charEnd"),
                "score": c.get("score"),
            }
            add_ev(sec, ev)
            if len(sec["evidence"]) >= max_per_section:
                break

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


# -----------------------------
# Plain-English finding synthesis (deterministic)
# -----------------------------
_SENT_SPLIT_RE = re.compile(r"(?<=[\.\!\?])\s+|\n+")

def _best_signal_sentence(text: str, max_len: int = 260) -> str:
    """
    Pick a concise sentence that contains obligation/compliance signals.
    Deterministic: no LLM, just regex + sentence scan.
    """
    t = _normalize_text(text or "").strip()
    if not t:
        return ""
    # break into sentences-ish
    parts = [p.strip() for p in _SENT_SPLIT_RE.split(t) if p and p.strip()]
    if not parts:
        return t[:max_len]
    # prefer sentences that contain obligation/compliance signals
    scored = []
    for p in parts:
        sig = _evidence_signal_score(p)
        # mild preference for medium-length sentences
        length_penalty = 0
        if len(p) < 40:
            length_penalty = 1
        if len(p) > 320:
            length_penalty = 2
        scored.append((sig, -length_penalty, -len(p), p))
    scored.sort(reverse=True)
    best = scored[0][3]
    best = best.replace("\r", " ").replace("\n", " ").strip()
    return best if len(best) <= max_len else (best[:max_len].rstrip() + "...")

def _why_it_matters(section_id: str, sentence: str) -> str:
    """
    Deterministic plain-English impact message per section.
    """
    sid = (section_id or "").strip().lower()
    s = (sentence or "").lower()
    if sid == "mission-objective":
        return "This explains what success looks like. If it is vague, the proposal and delivery plan can miss the mark."
    if sid == "scope-of-work":
        return "This drives cost and staffing. If tasks are bigger than expected, you will take schedule and margin risk."
    if sid == "deliverables-timelines":
        return "This affects your delivery plan. Missing a required deliverable or due date can make you non-compliant."
    if sid == "security-compliance-hosting-constraints":
        return "These requirements can force architecture changes and add compliance work (ATO/RMF, logging, encryption)."
    if sid == "eligibility-personnel-constraints":
        return "These constraints can block staffing (clearance/citizenship) or limit subcontractor options."
    if sid == "legal-data-rights-risks":
        return "These terms can create long-term legal exposure (data rights, audits, penalties, flowdowns)."
    if sid == "financial-risks":
        return "These terms affect cash flow and profitability (ceilings, reporting, invoicing cadence, overrun notice)."
    if sid == "submission-instructions-deadlines":
        return "This affects proposal compliance. Wrong format or missed instructions can get you rejected."
    if sid == "contradictions-inconsistencies":
        return "Conflicting requirements cause rework and risk. These should be clarified before committing."
    if sid == "gaps-questions-for-the-government":
        return "These are open questions. Unanswered items are proposal risk and should be clarified early."
    if sid == "recommended-internal-actions":
        return "These are concrete next steps to reduce risk and produce an accurate, compliant response."

    # overview
    if sid == "overview":
        if any(x in s for x in ["shall", "must", "required", "prohibited"]):
            return "This looks like a binding requirement. Confirm it early and assign an owner."
        return "This is a key point to validate and assign to the right team."

    return "This is relevant to delivery/compliance. Confirm and assign an owner."

def _plain_finding_from_evidence(section_id: str, ev_text: str) -> List[str]:
    """
    Return 2 bullets: Requirement/Key point + Why it matters.
    Deterministic and derived from evidence only.
    """
    sent = _best_signal_sentence(ev_text or "")
    if not sent:
        return []
    bullets: List[str] = []
    # use "Requirement" when obligation signal present, otherwise "Key point"
    label = "Requirement" if _has_obligation_signal(sent) else "Key point"
    bullets.append(f"{label}: {sent}")
    bullets.append(f"Why it matters: {_why_it_matters(section_id, sent)}")
    return bullets

def _format_evidence_bullet(prefix: str, ev: Dict[str, Any]) -> str:
    doc = ev.get("doc") or ev.get("docId") or "UnknownDoc"
    cs = ev.get("charStart")
    ce = ev.get("charEnd")
    snippet = (ev.get("text") or "").strip().replace("\r", " ").replace("\n", " ")
    snippet = snippet[:220]
    return f"{prefix}: {snippet} (Doc: {doc} span: {cs}-{ce})"


# -----------------------------
# Deterministic section confidence (no LLM)
# -----------------------------
def _clamp01(x: float) -> float:
    try:
        if x < 0.0:
            return 0.0
        if x > 1.0:
            return 1.0
        return float(x)
    except Exception:
        return 0.0


def _confidence_for_section(section: Dict[str, Any]) -> Tuple[str, int]:
    """
    Returns (confidence_label, confidence_pct).

    Inputs:
      - evidence count
      - evidence signal strength (via _evidence_signal_score)

    Output:
      confidence: missing | weak | moderate | strong
      confidence_pct: 0-100
    """
    ev_list = section.get("evidence") or []
    if not isinstance(ev_list, list) or len(ev_list) == 0:
        return ("missing", 0)

    # Evidence count factor: saturate at 6 evidence snippets
    ev_count = len(ev_list)
    count_factor = _clamp01(ev_count / 6.0)

    # Signal factor: average positive signal across evidence (cap at 5)
    sigs: List[float] = []
    for ev in ev_list:
        txt = ""
        if isinstance(ev, dict):
            txt = str(ev.get("text") or "")
        else:
            try:
                txt = str(getattr(ev, "text", "") or "")
            except Exception:
                txt = ""
        sig = _evidence_signal_score(txt)
        sigs.append(max(0.0, float(sig)))

    avg_sig = (sum(sigs) / max(1.0, float(len(sigs)))) if sigs else 0.0
    signal_factor = _clamp01(avg_sig / 5.0)

    # Weighted score: prefer count slightly more than signal
    pct = int(round(100.0 * _clamp01(0.60 * count_factor + 0.40 * signal_factor)))

    # Bucket
    if pct >= 80:
        label = "strong"
    elif pct >= 55:
        label = "moderate"
    else:
        label = "weak"

    return (label, pct)


def _apply_confidence(sections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    for sec in sections or []:
        if not isinstance(sec, dict):
            continue
        label, pct = _confidence_for_section(sec)
        sec["confidence"] = label
        sec["confidence_pct"] = pct
    return sections

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

    def short(s: str, n: int = 160) -> str:
        s = (s or "").replace("\r", " ").replace("\n", " ").strip()
        return s if len(s) <= n else (s[:n] + "...")

    if sid == "security-compliance-hosting-constraints":
        return "Security/compliance constraint found. Confirm IL level (Impact Level), RMF/ATO split, logging/CONMON, and any prohibited actions. Evidence: " + short(t)
    if sid == "legal-data-rights-risks":
        return "Legal/data-rights obligation found. Confirm IP/data rights, audit rights, disclosure penalties, and flowdowns. Evidence: " + short(t)
    if sid == "financial-risks":
        return "Financial obligation found. Confirm burn-rate reporting, overruns notification, ceilings, and invoicing cadence. Evidence: " + short(t)
    if sid == "deliverables-timelines":
        return "Deliverable/acceptance requirement found. Confirm CDRLs, due dates, acceptance criteria, and Government approval gates. Evidence: " + short(t)
    if sid == "eligibility-personnel-constraints":
        return "Staffing/eligibility constraint found. Confirm citizenship/clearance requirements, subcontractor restrictions, and access constraints. Evidence: " + short(t)
    if sid == "submission-instructions-deadlines":
        return "Submission requirement found. Confirm format, due dates, delivery mechanism, and required attachments. Evidence: " + short(t)
    if sid == "contradictions-inconsistencies":
        return "Potential ambiguity/inconsistency. Confirm conflicting requirements, undefined terms, or evolving document lists. Evidence: " + short(t)
    if sid == "scope-of-work":
        return "Scope/task requirement found. Confirm who does what, required tasks, and dependencies/assumptions. Evidence: " + short(t)
    if sid == "mission-objective":
        return "Mission/objective may be unclear in retrieved text. Confirm purpose/goal language and success criteria from the solicitation. Evidence: " + short(t)

    return "Requirement language found. Confirm scope, acceptance gates, and Gov vs Contractor responsibilities. Evidence: " + short(t)


def _backfill_sections_from_evidence(
    sections: List[Dict[str, Any]],
    intent: str,
) -> List[Dict[str, Any]]:
    intent = (intent or "strict_summary").strip().lower()
    is_triage = intent == "risk_triage"

    for sec in sections:
        sid = (sec.get("id") or "").strip().lower()
        findings = sec.get("findings") or []
        gaps = sec.get("gaps") or []
        actions = sec.get("recommended_actions") or []
        evidence = sec.get("evidence") or []

        sec["findings"] = list(findings)
        sec["gaps"] = list(gaps)
        sec["recommended_actions"] = list(actions)
        sec["evidence"] = list(evidence)

        if sec["evidence"]:
            sec["gaps"] = [g for g in sec["gaps"] if "Insufficient evidence retrieved" not in str(g)]

        kw = _section_keywords(sid)

        if sec["evidence"] and not sec["findings"]:
            kept = 0
            for ev in sec["evidence"]:
                if not _text_matches_keywords(ev.get("text") or "", kw) and sid != "overview":
                    continue
                sec["findings"].extend(_plain_finding_from_evidence(sid, ev.get("text") or ""))
                kept += 1
                if kept >= 3:
                    break

            if not sec["findings"] and sid != "overview":
                sec["findings"].append(
                    "GAP: Retrieved text looks off-topic for this section (often definitions/glossary). Request the specific contract section for this topic and rerun."
                )
                sec["gaps"].append("Retrieved evidence appears non-topical (often definitions/glossary). Recommend targeted retrieval and rerun analysis.")

        if is_triage and sec["evidence"]:
            ev0 = (sec["evidence"][0].get("text") or "").lower()
            if any(x in ev0 for x in ["shall", "must", "required", "prohibited"]) and sid not in ("overview",):
                sec["findings"].append(
                    f"POTENTIAL RISK (Owner: {_owner_for_section(sid)}): "
                    + _risk_blurb_for_section(sid, sec["evidence"][0].get("text") if sec["evidence"] else "")
                )

        if (not sec["findings"]) and (not sec["evidence"]):
            if not sec["gaps"]:
                sec["gaps"].append("Insufficient evidence retrieved for this section. Confirm relevant contract sections and rerun analysis.")
            if not sec["recommended_actions"]:
                sec["recommended_actions"].append("Request the relevant section(s) from the Government/PM and rerun RAG triage.")

        if sid == "security-compliance-hosting-constraints" and sec["evidence"] and not sec["recommended_actions"]:
            sec["recommended_actions"].append("Confirm IL level, ATO boundary responsibilities (Gov vs Contractor), and required RMF artifacts/acceptance gates.")
        if sid == "deliverables-timelines" and sec["evidence"] and not sec["recommended_actions"]:
            sec["recommended_actions"].append("Extract deliverables/approval gates into a tracker (CDRLs, cadence, acceptance criteria) and confirm IMS requirements.")
        if sid == "eligibility-personnel-constraints" and sec["evidence"] and not sec["recommended_actions"]:
            sec["recommended_actions"].append("Confirm staffing/citizenship/clearance/flowdown constraints and ensure subcontractor vetting language is feasible.")

    return sections


def _strengthen_overview_from_evidence(sections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ov = None
    for s in sections:
        if (s.get("id") or "").strip().lower() == "overview":
            ov = s
            break
    if ov is None:
        return sections

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

    ov.setdefault("findings", [])
    existing = ov.get("findings") or []
    ov["findings"] = list(existing)

    if not any(str(x).startswith("EVIDENCE:") for x in ov["findings"]):
        injected: List[str] = []
        added = 0
        for ev in pool:
            if _is_glossary_text(ev.get("text") or "") and not _has_obligation_signal(ev.get("text") or ""):
                continue
            injected.append(_format_evidence_bullet("EVIDENCE", ev))
            added += 1
            if added >= 3:
                break
        ov["findings"] = injected + ov["findings"]

    if len(ov["findings"]) < 6:
        added = 0
        for ev in pool:
            if _is_glossary_text(ev.get("text") or "") and not _has_obligation_signal(ev.get("text") or ""):
                continue
            ov["findings"].append(_format_evidence_bullet("EVIDENCE", ev))
            added += 1
            if added >= 6:
                break

    if not ov.get("recommended_actions"):
        ov["recommended_actions"] = [
            "Review the obligations/constraints below and assign owners (Security/ISSO, PM, Engineering, Legal, Finance) for validation and response planning."
        ]

    return sections


# -----------------------------
# Main entry
# -----------------------------
def _materialize_risks_from_sections(sections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Deterministically materialize Risk Register items from section findings.

    Inputs:
      - sections[*].findings[] lines that start with:
          POTENTIAL RISK ...
          POTENTIAL INFERRED RISK ...
          POTENTIAL RISK (Owner: X): Title...
    Output:
      - stable id (hash), clean title, clean owner
    """
    import hashlib
    import re

    out: List[Dict[str, Any]] = []

    owner_re = re.compile(r"\(Owner:\s*([^)]+)\)\s*:\s*(.*)$", re.I)

    for sec in (sections or []):
        sid = str(sec.get("id") or "").strip() or None
        sec_owner = str(sec.get("owner") or "").strip() or None

        for line in (sec.get("findings") or []):
            raw = str(line or "").strip()
            if not raw:
                continue

            low = raw.lower()
            if not (low.startswith("potential risk") or low.startswith("potential inferred risk")):
                continue

            owner = sec_owner
            title = raw

            # Try the structured "(Owner: X): Title..." shape
            m = owner_re.search(raw)
            if m:
                owner = (m.group(1) or "").strip() or owner
                title = (m.group(2) or "").strip() or title
            else:
                # Fallback: everything after first ":" if present
                if ":" in raw:
                    title = raw.split(":", 1)[1].strip()

            # Normalize title: remove trailing "Evidence: ..." to keep it readable
            if " evidence:" in title.lower():
                title = title.split("Evidence:", 1)[0].strip()

            if not title:
                continue

            # Stable id: section + normalized title
            h = hashlib.sha1(f"{sid or 'unknown'}|{title}".encode("utf-8")).hexdigest()[:12]
            rid = f"{sid or 'unknown'}:{h}"

            out.append(
                {
                    "id": rid,
                    "title": title,
                    "severity": "Medium",
                    "owner": owner,
                    "confidence": "Inferred" if low.startswith("potential inferred risk") else "Moderate",
                    "source_type": "DETERMINISTIC",
                    "source_confidence_tier": 2,
                    "section_id": sid,
                    "evidence_ids": [],
                    "flag_ids": [],
                    "rationale": raw,
                    "source": ["sections"],
                }
            )

    # de-dupe by id
    seen = set()
    deduped: List[Dict[str, Any]] = []
    for r in out:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        deduped.append(r)
    return deduped
def _normalize_owner_team(owner: Optional[str]) -> str:
    o = (owner or "").strip()
    allowed = {"Security/ISSO", "Legal/Contracts", "Program/PM", "Engineering", "Finance", "QA", "Unassigned"}
    return o if o in allowed else ""


def _owner_from_risk_meta(*, ownerTeam: Optional[str], category: Optional[str], label: str, fallback: str = "Unassigned") -> str:
    """
    Deterministic owner mapping (no guessing, no LLM).
    Priority:
      1) explicit ownerTeam if valid
      2) category-based mapping
      3) keyword mapping from label
      4) fallback
    """
    explicit = _normalize_owner_team(ownerTeam)
    if explicit:
        return explicit

    c = (category or "").strip().lower()
    t = (label or "").strip().lower()

    # Category-based mapping (extend over time)
    if any(x in c for x in ["dfars", "rmf", "ato", "cui", "cmmc", "nist", "fedramp", "il"]):
        return "Security/ISSO"
    if any(x in c for x in ["ip", "data_rights", "rights", "legal", "audit", "nda"]):
        return "Legal/Contracts"
    if any(x in c for x in ["invoice", "payment", "ceiling", "pricing", "funding", "rom"]):
        return "Finance"
    if any(x in c for x in ["deliverable", "cdrl", "schedule", "milestone", "submission"]):
        return "Program/PM"

    # Keyword mapping (extend over time)
    if any(x in t for x in ["rmf", "ato", "il", "cui", "dfars", "nist", "800-53", "800-171", "incident", "breach", "encryption", "logging", "conmon", "zero trust"]):
        return "Security/ISSO"
    if any(x in t for x in ["data rights", "ip", "license", "audit rights", "nda", "disclosure", "penalty", "flowdown"]):
        return "Legal/Contracts"
    if any(x in t for x in ["invoice", "invoicing", "payment", "ceiling", "overrun", "funding", "burn rate"]):
        return "Finance"
    if any(x in t for x in ["cdrl", "deliverable", "submission", "deadline", "milestone", "ims", "schedule"]):
        return "Program/PM"
    if any(x in t for x in ["test", "acceptance", "qa", "inspection"]):
        return "QA"

    return _normalize_owner_team(fallback) or "Unassigned"

def _materialize_risks_from_flags(review: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Highest-confidence risk materialization.

    Source:
      - review.autoFlags.hits (deterministic flags engine)
    Tiering:
      - source_type=FLAG
      - source_confidence_tier=3  (highest confidence)
    Provenance:
      - source_refs.hit_keys / flag_ids / doc_ids
    """
    import hashlib

    auto = (review or {}).get("autoFlags") or {}
    hits = auto.get("hits") or []

    out: List[Dict[str, Any]] = []
    seen = set()

    for h in hits:
        if not isinstance(h, dict):
            try:
                h = h.model_dump()
            except Exception:
                continue

        hit_key = str(h.get("hit_key") or "").strip()
        flag_id = str(h.get("id") or h.get("flag_id") or h.get("flagId") or "").strip()
        label = str(h.get("label") or flag_id or "Flag hit").strip()

        doc_id = str(h.get("docId") or "").strip() or None
        doc_name = str(h.get("docName") or "").strip() or None
        line = h.get("line")

        # Stable ID: prefer hit_key; else hash of key fields
        if hit_key:
            rid = f"flag:{hit_key}"
        else:
            base = f"{flag_id}|{doc_id}|{line}|{label}"
            rid = "flag:" + hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]

        if rid in seen:
            continue
        seen.add(rid)

        sev = str(h.get("severity") or "Medium").strip()

        # Owner mapping can be improved later (flag namespace -> owner)
        owner = _owner_from_risk_meta(ownerTeam=h.get("ownerTeam"), category=h.get("category"), label=label, fallback="Security/ISSO")
        snippet = (h.get("snippet") or h.get("match") or "")
        snippet = str(snippet).strip()

        if doc_name and line is not None:
            rationale = f"[{doc_name}] line={line} :: {snippet}"
        else:
            rationale = snippet or label

        out.append(
            {
                "id": rid,
                "title": label,
                "severity": sev,
                "owner": owner,
                "section_id": None,

                # provenance + tier
                "confidence": "High",
                "source_type": "FLAG",
                "source_confidence_tier": 3,
                "source_refs": {
                    "hit_keys": [hit_key] if hit_key else [],
                    "flag_ids": [flag_id] if flag_id else [],
                    "doc_ids": [doc_id] if doc_id else [],
                },

                "evidence_ids": [],
                "flag_ids": [flag_id] if flag_id else [],
                "rationale": rationale,
            }
        )

    return out


def _materialize_risks_from_heuristic_hits(heuristic_hits: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """
    Tier-2 deterministic risk materialization.

    Source:
      - RagAnalyzeRequest.heuristic_hits
    Tiering:
      - source_type=HEURISTIC
      - source_confidence_tier=2
    """
    import hashlib

    hits = heuristic_hits or []
    out: List[Dict[str, Any]] = []
    seen = set()

    for h in hits:
        if not isinstance(h, dict):
            try:
                h = h.model_dump()
            except Exception:
                continue

        hit_key = str(h.get("hit_key") or "").strip()
        heur_id = str(h.get("id") or "").strip()
        label = str(h.get("label") or heur_id or "Heuristic hit").strip()

        doc_id = str(h.get("docId") or "").strip() or None
        doc_name = str(h.get("docName") or "").strip() or None
        line = h.get("line")

        if hit_key:
            rid = f"heur:{hit_key}"
        else:
            base = f"{heur_id}|{doc_id}|{line}|{label}"
            rid = "heur:" + hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]

        if rid in seen:
            continue
        seen.add(rid)

        sev = str(h.get("severity") or "Medium").strip()
        owner = _owner_from_risk_meta(ownerTeam=h.get("ownerTeam"), category=h.get("category"), label=label, fallback="Unassigned")
        snippet = str((h.get("snippet") or h.get("match") or "")).strip()
        if doc_name and line is not None:
            rationale = f"[{doc_name}] line={line} :: {snippet}"
        else:
            rationale = snippet or label

        out.append(
            {
                "id": rid,
                "title": label,
                "severity": sev,
                "owner": owner,
                "section_id": None,
                "confidence": "Moderate",
                "source_type": "HEURISTIC",
                "source_confidence_tier": 2,
                "source_refs": {
                    "hit_keys": [hit_key] if hit_key else [],
                    "heuristic_ids": [heur_id] if heur_id else [],
                    "doc_ids": [doc_id] if doc_id else [],
                },
                "evidence_ids": [],
                "flag_ids": [],
                "rationale": rationale,
            }
        )

    return out

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
    heuristic_hits: Optional[List[Dict[str, Any]]] = None,
    enable_inference_risks: bool = True,
    inference_candidates: Optional[List[str]] = None,
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

    section_question_map = _question_section_map(intent)
    questions = [q for (_sec, q) in section_question_map]

    effective_top_k = _effective_top_k(top_k, profile)
    retrieved: Dict[str, List[Dict[str, Any]]] = {}
    citations: List[Dict[str, Any]] = []

    t_ret0 = time.time() if _timing_enabled() else 0.0
    retrieved, context, max_chars, signals = retrieve_context(
        vector=vector,
        llm=llm,
        questions=questions,
        effective_top_k=effective_top_k,
        filters={'review_id': review_id},
        snippet_cap=_effective_snippet_chars(profile),
        intent=intent,
        profile=profile,
        query_review_fn=query_review,
        env_get_fn=_env,
        effective_context_chars_fn=_effective_context_chars,
        heuristic_hits=heuristic_hits,
    )

    # --- Deterministic signals injection (risk_triage only) -----------------
    # signals are NOT contract evidence; they are deterministic hints:
    #  - flags (autoFlags), heuristics, and other non-RAG indicators.
    # They should never be cited as "contract text".
    if str(intent or "").strip().lower() == "risk_triage":
        try:
            # Tier 3 signals from autoFlags (deterministic, highest confidence)
            try:
                _flag_signals = []
                try:
                    _reviews = _read_reviews_file(storage)
                    _rev = next((r for r in (_reviews or []) if str(r.get("id")) == str(review_id)), None) or {}
                except Exception:
                    _rev = {}
                _af = (_rev.get("autoFlags") or {}) if isinstance(_rev, dict) else {}
                _hits = _af.get("hits") or []
                for fh in (_hits or []):
                    if not isinstance(fh, dict):
                        continue
                    _fid = str(fh.get("id") or fh.get("hit_key") or fh.get("key") or "").strip()
                    _flabel = str(fh.get("label") or fh.get("name") or "").strip()
                    _fsev = str(fh.get("severity") or "").strip()
                    if not (_fid or _flabel):
                        continue
                    _flag_signals.append({
                        "id": _fid or _flabel,
                        "label": _flabel or _fid,
                        "severity": _fsev,
                        "source": "autoFlag",
                        "why": "",
                    })
            except Exception:
                _flag_signals = []

            _sig_items = (_flag_signals + (signals if isinstance(signals, list) else []))
            if _sig_items:
                _sig_lines = []
                for s in _sig_items:
                    if not isinstance(s, dict):
                        continue
                    _sid = str(s.get("id") or s.get("key") or "").strip()
                    _label = str(s.get("label") or s.get("name") or s.get("type") or "").strip()
                    _sev = str(s.get("severity") or s.get("level") or "").strip()
                    _src = str(s.get("source") or s.get("origin") or "").strip()
                    _why = str(s.get("why") or s.get("rationale") or s.get("reason") or "").strip()
                    parts = []
                    if _label: parts.append(_label)
                    if _sid: parts.append(f"id={_sid}")
                    if _sev: parts.append(f"sev={_sev}")
                    if _src: parts.append(f"src={_src}")
                    line = " - " + " | ".join(parts) if parts else None
                    if line:
                        if _why:
                            line = line + f" | why={_why}"
                        _sig_lines.append(line)

                if _sig_lines:
                    _sig_block = (
                        "===BEGIN DETERMINISTIC SIGNALS (NOT CONTRACT EVIDENCE)===\n"
                        + "\n".join(_sig_lines)
                        + "\n===END DETERMINISTIC SIGNALS===\n"
                    )
                    context = (context or "") + "\n\n" + _sig_block
        except Exception:
            pass
    # -----------------------------------------------------------------------
    # Debug: persist the final prompt context (includes deterministic signals)
    # NOTE: this is ONLY for debug/testing; do not treat as contract evidence.
    if debug:
        try:
            if not isinstance(stats, dict):
                stats = {}
        except Exception:
            stats = {}
        try:
            stats['debug_context'] = context or ''
        except Exception:
            pass
    if _timing_enabled():
        print('[RAG] retrieval done', round(time.time() - t_ret0, 2), 's')

    # Prompt
    if intent == "risk_triage":
        # PROMPT/CONTEXT CLAMPS (prevent Ollama truncation; deterministic)
        try:
            _ctx_max = int(os.getenv("RAG_CONTEXT_MAX_CHARS", "9000") or "9000")
        except Exception:
            _ctx_max = 9000
        try:
            _prompt_max = int(os.getenv("RAG_PROMPT_MAX_CHARS", "14000") or "14000")
        except Exception:
            _prompt_max = 14000
        if isinstance(context, str) and _ctx_max > 0 and len(context) > _ctx_max:
            context = context[:_ctx_max]
        
        prompt = (
            "OVERVIEW\n"
            "Write a short executive brief of this review, section-by-section.\n"
            "Audience: non-lawyer, busy exec.\n\n"
            "HARD RULES\n"
            "- Plain text only. No markdown.\n"
            "- Do NOT fabricate facts.\n"
            f"- If evidence is insufficient for a section, write exactly: \"{INSUFFICIENT}\"\n"
            "- Keep each bullet short and concrete.\n\n"
            "FORMAT\n"
            "- Use the SECTION HEADERS exactly as listed, in order.\n"
            "- Under each section, output ONLY findings as bullets (3ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Å“6 bullets max).\n"
            "- Do NOT include any Evidence lines (server will attach evidence separately).\n\n"
            "SECTIONS (exact order)\n"
            + "\n".join(RAG_REVIEW_SUMMARY_SECTIONS)
            + "\n\n"
            "RETRIEVED CONTEXT\n"
            f"{context}\n"
        ).strip()  # STRICT_SUMMARY_FAST_PROMPT
    else:
        prompt = (
            "OVERVIEW\n"
            "Write ONE unified cross-document executive brief for this review.\n"
            "It must read like normal English for a non-lawyer.\n\n"
            "HARD RULES\n"
            "- Plain text only. No markdown.\n"
            "- Do NOT output bracket placeholders like \"[insert ...]\".\n"
            f"- If you cannot find evidence for a section, write exactly: \"{INSUFFICIENT}\"\n"
            "- Do not fabricate deliverables, dates, roles, responsibilities, or requirements.\n"
            "- Write in plain English. Avoid contract/legal phrasing.\n"
            "- If you use an acronym (IL5, RMF, ATO, CUI, DFARS, SBOM, CONMON), define it the first time in parentheses.\n"
            "- You MUST use evidence from the retrieved context only.\n"
            "- Evidence MUST be copied only from within blocks between:\n"
            "  ===BEGIN CONTRACT EVIDENCE=== and ===END CONTRACT EVIDENCE===\n"
            "- Do NOT treat QUESTION lines or instructions as evidence.\n\n"
            "FORMAT RULES\n"
            "- Use the SECTION HEADERS exactly as listed below, in the exact order.\n"
            "- For EACH SECTION:\n"
            "  1) Findings (bullets, plain English, add 'why it matters')\n"
            "  2) Evidence: 1-3 short snippets copied EXACTLY from retrieved context\n"
            "  3) If insufficient evidence, state what to retrieve/clarify\n\n"
            "SECTIONS (exact order)\n"
            + "\n".join(RAG_REVIEW_SUMMARY_SECTIONS)
            + "\n\n"
            "RETRIEVED CONTEXT\n"
            f"{context}\n"
        ).strip()

    # POST-BUILD PROMPT CLAMP (applies to both prompt branches; deterministic)
    try:
        _prompt_max = int(os.getenv("RAG_PROMPT_MAX_CHARS", "14000") or "14000")
    except Exception:
        _prompt_max = 14000
    if isinstance(prompt, str) and _prompt_max > 0 and len(prompt) > _prompt_max:
        prompt = prompt[:_prompt_max]
    
    t_gen0 = time.time() if _timing_enabled() else 0.0
    if _timing_enabled():
        print("[RAG] generation start")

    # DEBUG: prompt + context size
    if _timing_enabled():
        try:
            print('[RAG] prompt_len=', len(prompt))
            print('[RAG] context_len=', len(context))
        except Exception:
            pass
    
    # STRICT TOKENS OVERRIDE (strict_summary only; restore after call)
    summary_raw = ""
    _old_llm_max = None
    if intent != "risk_triage":
        try:
            _old_llm_max = os.getenv("LLM_MAX_TOKENS")
            os.environ["LLM_MAX_TOKENS"] = str(os.getenv("RAG_STRICT_MAX_TOKENS", "48") or "48")
        except Exception:
            pass
    try:
        summary_raw = (llm.generate(prompt) or {}).get("text") or ""
    finally:
        if intent != "risk_triage":
            try:
                if _old_llm_max is None:
                    os.environ.pop("LLM_MAX_TOKENS", None)
                else:
                    os.environ["LLM_MAX_TOKENS"] = _old_llm_max
            except Exception:
                pass
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
        intent=intent,
        section_question_map=section_question_map,
        citations=citations,
        retrieved=retrieved,
    )

    # Populate per-section owner for UI routing (dict OR Pydantic model)
    for section in parsed_sections:
        if isinstance(section, dict):
            sid_val = section.get("id")
        else:
            sid_val = getattr(section, "id", None)

        sid = (sid_val or "").strip().lower()
        owner = _owner_for_section(sid)

        if isinstance(section, dict):
            section["owner"] = owner
        else:
            try:
                setattr(section, "owner", owner)
            except Exception:
                pass

    parsed_sections = _backfill_sections_from_evidence(parsed_sections, intent)
    parsed_sections = _strengthen_overview_from_evidence(parsed_sections)
    parsed_sections = _apply_confidence(parsed_sections)

    
    # NORMALIZE SECTION OUTPUTS (clean findings, keep evidence structured)
    for _s in (parsed_sections or []):
        try:
            _normalize_section_outputs(_s)
        except Exception:
            pass
    
    if _timing_enabled():
        print("[RAG] generation done", round(time.time() - t_gen0, 2), "s")
        print("[RAG] analyze done", round(time.time() - t0, 2), "s")
        
        # FINAL RETURN GUARD (never return None ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â required by RagAnalyzeResponse.model_validate)
        # If the function path forgets to return, we still emit a contract-shaped dict.
        out = {
            "review_id": str(review_id),
            "mode": str(mode),
            "top_k": int(locals().get("effective_top_k") or locals().get("top_k") or 12),
            "analysis_intent": str(locals().get("intent") or locals().get("analysis_intent") or "strict_summary"),
            "context_profile": str(locals().get("context_profile") or "fast"),
            "summary": (locals().get("summary") or locals().get("summary_text") or ""),
            "citations": (locals().get("citations") or []),
            "retrieved_counts": (locals().get("retrieved_counts") or {}),
            "risks": (locals().get("risks") or []),
            "sections": (locals().get("parsed_sections") or locals().get("sections") or []),
            "stats": (locals().get("stats") or None),
            "warnings": (locals().get("warnings") or []),
        }
        # return handled by final return (do not early-return on timing)
        

    retrieved_total = sum(len(retrieved.get(q) or []) for q in questions)
    warnings: List[str] = []

    zero_hit_questions = [q for q in questions if len(retrieved.get(q) or []) == 0]
    if zero_hit_questions:
        warnings.append(f"Insufficient evidence for {len(zero_hit_questions)} section(s).")

    context_used_chars = len(context)
    context_truncated = bool(context_used_chars >= max_chars)
    if context_truncated:
        warnings.append(f"Context truncated at {max_chars} chars.")

    # Deterministic: materialize risks for UI (Risk Register) from server truth.
    risks, risk_counts = materialize_risk_register(
        storage=storage,
        review_id=str(review_id),
        intent=str(intent),
        parsed_sections=(parsed_sections or []),
        heuristic_hits=heuristic_hits,
        enable_inference_risks=bool(enable_inference_risks),
        inference_candidates=inference_candidates,
        read_reviews_fn=_read_reviews_file,
        materialize_flags_fn=_materialize_risks_from_flags,
        materialize_heuristics_fn=_materialize_risks_from_heuristic_hits,
        materialize_sections_fn=_materialize_risks_from_sections,
        materialize_inference_fn=_materialize_risks_from_inference,
    )

    # Ensure stats exists so runtime truth can be observed at the API boundary.
    # (response_model_exclude_none drops stats when None.)
    try:
        if not isinstance(locals().get("stats"), dict):
            stats = {}
        stats["risk_objects"] = risk_counts
    except Exception:
        # Never fail the endpoint due to stats plumbing
        pass
    if debug:
        try:
            print('[RAG][RISKS]', risk_counts)
        except Exception:
            pass

    # FINAL RETURN (authoritative): always return contract-shaped response.
    # Timing logs are optional, but returning is not.
    out = build_rag_response_dict(
        review_id=str(review_id),
        mode=str(mode),
        effective_top_k=int(locals().get('effective_top_k') or locals().get('top_k') or 12),
        intent=str(locals().get('intent') or locals().get('analysis_intent') or 'strict_summary'),
        context_profile=str(locals().get('context_profile') or 'fast'),
        summary=(locals().get('summary') or locals().get('summary_text') or ''),
        citations=(locals().get('citations') or []),
        retrieved_counts=(locals().get('retrieved_counts') or {}),
        risks=(locals().get('risks') or []),
        sections=(locals().get('parsed_sections') or locals().get('sections') or []),
        stats=(locals().get('stats') or None),
        warnings=(locals().get('warnings') or []),
    )
    return out
    # 4) Inference risks (Tier 1, lowest confidence) - optional via env toggle






def _materialize_risks_from_inference(
    sections: List[Dict[str, Any]],
    *,
    enable_inference_risks: bool = True,
    inference_candidates: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Tier-1 candidate risks (lowest confidence).

    Default: ON.

    Disable:
      - per request: enable_inference_risks=False
      - global kill switch: RAG_DISABLE_INFERENCE_RISKS=1
    """
    import hashlib

    if (not enable_inference_risks) or (_env("RAG_DISABLE_INFERENCE_RISKS", "0").strip() == "1"):
        return []

    out: List[Dict[str, Any]] = []
    seen = set()

    # Deterministic injected candidates (preferred for smoke/UI)
    for raw0 in (inference_candidates or []):
        raw = str(raw0 or "").strip()
        if not raw:
            continue
        h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
        rid = f"infer:inject:{h}"
        if rid in seen:
            continue
        seen.add(rid)
        out.append(
            {
                "id": rid,
                "title": raw,
                "severity": "Low",
                "owner": "Unassigned",
                "section_id": None,
                "confidence": "Speculative",
                "source_type": "INFERENCE",
                "source_confidence_tier": 1,
                "source_refs": {"injected": True},
                "evidence_ids": [],
                "flag_ids": [],
                "rationale": raw,
            }
        )

    def is_candidate(line: str) -> bool:
        t = (line or "").strip()
        if not t:
            return False
        tl = t.lower()

        # Don't duplicate deterministic markers
        if tl.startswith("potential risk") or tl.startswith("potential inferred risk"):
            return False

        # Only accept explicit uncertainty language
        return ("potential concern" in tl) or ("might be a risk" in tl) or ("may be a risk" in tl)

    for sec in (sections or []):
        if not isinstance(sec, dict):
            continue

        sid = str(sec.get("id") or "").strip() or "unknown"
        owner = str(sec.get("owner") or "Unassigned").strip() or "Unassigned"

        findings = sec.get("findings") or []
        if not isinstance(findings, list):
            continue

        ev_doc_ids: List[str] = []
        for ev in (sec.get("evidence") or []):
            if isinstance(ev, dict):
                did = str(ev.get("docId") or "").strip()
                if did:
                    ev_doc_ids.append(did)

        for f in findings:
            raw = str(f or "").strip()
            if not is_candidate(raw):
                continue

            h = hashlib.sha1(f"{sid}|{raw}".encode("utf-8")).hexdigest()[:12]
            rid = f"infer:{sid}:{h}"
            if rid in seen:
                continue
            seen.add(rid)

            out.append(
                {
                    "id": rid,
                    "title": raw,
                    "severity": "Low",
                    "owner": owner,
                    "section_id": sid,
                    "confidence": "Speculative",
                    "source_type": "INFERENCE",
                    "source_confidence_tier": 1,
                    "source_refs": {
                        "section_id": sid,
                        "finding": raw,
                        "doc_ids": list(dict.fromkeys(ev_doc_ids))[:10],
                    },
                    "evidence_ids": [],
                    "flag_ids": [],
                    "rationale": raw,
                }
            )

    return out















