# rag/service.py
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

from core.config import PdfReader
from core.dynamo_meta import DynamoMeta
from providers.storage import StorageProvider
from providers.vectorstore import VectorStore
from rag.service_helpers import retrieve_context
from reviews.router import _read_reviews_file  # uses StorageProvider


# =============================================================================
# SECTION OUTPUT NORMALIZATION
# - Keep evidence in section.evidence[]
# - Keep findings[] as short bullets only
# =============================================================================

# Tuning knobs (UI ergonomics)
_SECTION_MAX_FINDINGS = 6  # clamp findings bullets per section (tight default)
_SECTION_MAX_EVIDENCE = 6  # clamp evidence snippets per section (tight default)
_FINDING_MAX_LEN = 160  # clamp bullet length (tight default)
_DROP_TRAILING_PERIODS = True  # normalize bullets by removing trailing periods

_EVIDENCE_LINE_RE = re.compile(
    r"^\s*EVIDENCE:\s*(?P<snippet>.+?)\s*\(Doc:\s*(?P<doc>.+?)\s*span:\s*(?P<cs>\d+)\-(?P<ce>\d+)\)\s*$",
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
        "docId": doc,  # best-effort
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
        s = "Ensure " + s[len("review and ensure ") :].lstrip()
    elif sl.startswith("review and verify "):
        s = "Verify " + s[len("review and verify ") :].lstrip()
    elif sl.startswith("review and assess "):
        s = "Assess " + s[len("review and assess ") :].lstrip()
    elif sl.startswith("review and understand "):
        s = "Understand " + s[len("review and understand ") :].lstrip()
    elif sl.startswith("review to ensure "):
        s = "Ensure " + s[len("review to ensure ") :].lstrip()
    elif sl.startswith("review to verify "):
        s = "Verify " + s[len("review to verify ") :].lstrip()

    # Normalize "X that ..." -> "X ..." (shorter)
    sl = s.lower()
    if sl.startswith("verify that "):
        s = "Verify " + s[len("verify that ") :].lstrip()
    elif sl.startswith("ensure that "):
        s = "Ensure " + s[len("ensure that ") :].lstrip()
    elif sl.startswith("confirm that "):
        s = "Confirm " + s[len("confirm that ") :].lstrip()

    # Remove trailing periods (UI consistency)
    if _DROP_TRAILING_PERIODS:
        s = s.rstrip()
        while s.endswith("."):
            s = s[:-1].rstrip()

    # Clamp length
    if len(s) > _FINDING_MAX_LEN:
        s = s[:_FINDING_MAX_LEN].rstrip() + "..."

    # Hardening: strip any non-ASCII (prevents mojibake leaking to UI)
    try:
        s = s.encode("ascii", "ignore").decode("ascii")
    except Exception:
        pass

    return s


def _clean_findings_line(s: str) -> Optional[str]:
    """
    Findings must be short bullets only (no scaffolding, no evidence blocks).
    """
    t = (s or "").strip()
    if not t:
        return None

    tl = t.lower()

    # Drop broad LLM intro sentence patterns
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

    if "based on the retrieved evidence" in tl and "actions" in tl:
        return None

    # Drop markdown-ish headers / department headers
    if tl.endswith("**") or tl.endswith(":**"):
        return None
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


# =============================================================================
# Contract: modes + defaults
# =============================================================================
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
    r'\("\.\.\."\)',
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


# =============================================================================
# Profile-driven caps
# =============================================================================
def _effective_top_k(req_top_k: int, context_profile: str) -> int:
    k = max(1, min(int(req_top_k or 12), 50))
    p = (context_profile or "fast").strip().lower()
    if p == "deep":
        return min(k, 40)
    if p == "balanced":
        return min(k, 18)
    if _fast_enabled():
        return min(k, 6)
    return min(k, 12)


def _effective_context_chars(context_profile: str) -> int:
    p = (context_profile or "fast").strip().lower()
    if p == "deep":
        return 60000
    if p == "balanced":
        return 24000
    if _fast_enabled():
        return int((_env("RAG_FAST_CONTEXT_MAX_CHARS", "9000") or "9000").strip() or "9000")
    return 18000


def _effective_snippet_chars(context_profile: str) -> int:
    p = (context_profile or "fast").strip().lower()
    if p == "deep":
        return 1400
    if p == "balanced":
        return 1000
    return 900


# =============================================================================
# Questions and routing
# =============================================================================
def _question_section_map(intent: str) -> List[Tuple[str, str]]:
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
            (
                "DELIVERABLES & TIMELINES",
                "Identify delivery/acceptance gates and required approvals (CDRLs, QA, test, acceptance criteria).",
            ),
            ("FINANCIAL RISKS", "Identify financial and invoicing risks (ceilings, overruns, payment terms, reporting cadence)."),
            ("DELIVERABLES & TIMELINES", "Identify schedule risks (IMS, milestones, reporting cadence, penalties)."),
            ("CONTRADICTIONS & INCONSISTENCIES", "Identify ambiguous/undefined terms and contradictions that require clarification."),
            ("OVERVIEW", "List top red-flag phrases/requirements with evidence and suggested internal owner (security/legal/PM/finance)."),
            ("MISSION & OBJECTIVE", "What is the mission and objective of this effort?"),
            ("SCOPE OF WORK", "What is the scope of work and required deliverables?"),
            ("SUBMISSION INSTRUCTIONS & DEADLINES", "What are submission instructions and deadlines, including required formats and delivery method?"),
            ("GAPS / QUESTIONS FOR THE GOVERNMENT", "What gaps require clarification from the Government?"),
            ("RECOMMENDED INTERNAL ACTIONS", "What internal actions should we take next (security/legal/PM/engineering/finance)?"),
        ]

    return [
        ("MISSION & OBJECTIVE", "What is the mission and objective of this effort?"),
        ("SCOPE OF WORK", "What is the scope of work and required deliverables?"),
        (
            "SECURITY, COMPLIANCE & HOSTING CONSTRAINTS",
            "What are the security, compliance, and hosting constraints (IL levels, NIST, DFARS, CUI, ATO/RMF, logging)?",
        ),
        (
            "ELIGIBILITY & PERSONNEL CONSTRAINTS",
            "What are the eligibility and personnel constraints (citizenship, clearances, facility, location, export controls)?",
        ),
        ("LEGAL & DATA RIGHTS RISKS", "What are key legal and data rights risks (IP/data rights, audit rights, flowdowns)?"),
        ("FINANCIAL RISKS", "What are key financial risks (pricing model, ceilings, invoicing systems, payment terms)?"),
        ("SUBMISSION INSTRUCTIONS & DEADLINES", "What are submission instructions and deadlines, including required formats and delivery method?"),
        ("CONTRADICTIONS & INCONSISTENCIES", "What contradictions or inconsistencies exist across documents?"),
        ("GAPS / QUESTIONS FOR THE GOVERNMENT", "What gaps require clarification from the Government?"),
        ("RECOMMENDED INTERNAL ACTIONS", "What internal actions should we take next (security/legal/PM/engineering/finance)?"),
    ]


# =============================================================================
# Sections parsing for UI
# =============================================================================
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
    raw = _normalize_text(text or "")
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

        mode: Optional[str] = None
        for ln in block_lines:
            t = (ln or "").strip()
            if not t:
                continue

            tl = t.lower()
            if tl.startswith("findings"):
                mode = "findings"
                continue
            if tl.startswith("evidence"):
                mode = "evidence"
                continue
            if tl.startswith("to verify") or tl.startswith("to clarify") or tl.startswith("gaps"):
                mode = "gaps"
                continue
            if tl.startswith("recommended") or tl.startswith("suggested owner") or tl.startswith("actions"):
                mode = "recommended_actions"
                continue

            is_bullet = t.startswith(("-", "*"))
            bullet_text = t.lstrip("-*").strip() if is_bullet else t

            if mode == "findings" or mode is None:
                sec["findings"].append(bullet_text)
            elif mode == "gaps":
                sec["gaps"].append(bullet_text)
            elif mode == "recommended_actions":
                sec["recommended_actions"].append(bullet_text)
            else:
                # evidence section from LLM is ignored (server attaches evidence deterministically)
                continue

        sections.append(sec)

    if not sections:
        sections = [
            {
                "id": "overview",
                "title": "OVERVIEW",
                "findings": [text.strip() or INSUFFICIENT],
                "evidence": [],
                "gaps": [],
                "recommended_actions": [],
            }
        ]
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
    section_question_map: List[Tuple[str, str]],
    citations: List[Dict[str, Any]],
    retrieved: Dict[str, List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    max_per_section = _env_int("RAG_EVIDENCE_MAX_PER_SECTION", 3)
    allow_glossary = _env_bool("RAG_EVIDENCE_ALLOW_GLOSSARY", False)
    min_signal = _env_int("RAG_EVIDENCE_MIN_SIGNAL", 1)

    for sec in sections:
        sec.setdefault("evidence", [])
        sec.setdefault("findings", [])
        sec.setdefault("gaps", [])
        sec.setdefault("recommended_actions", [])
        sec["_evidence_seen"] = set()

    sec_by_title = {(s.get("title") or "").strip(): s for s in sections}
    if not isinstance(citations, list):
        citations = []
    if not isinstance(retrieved, dict):
        retrieved = {}

    def add_ev(sec: Dict[str, Any], ev: Dict[str, Any]) -> None:
        seen = sec.get("_evidence_seen")
        if not isinstance(seen, set):
            seen = set()
            sec["_evidence_seen"] = seen
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
        if _is_glossary_text(text):
            if not allow_glossary and (not _has_obligation_signal(text)):
                return False
            if sec_id != "overview" and sig < min_signal:
                return False
        return sig >= min_signal

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
                "doc": meta.get("doc_name") or h.get("doc_name") or meta.get("doc_id"),
                "text": _extract_obligation_excerpt(chunk_text, max_len=1200),
                "charStart": meta.get("char_start"),
                "charEnd": meta.get("char_end"),
                "score": h.get("score"),
            }
            add_ev(sec, ev)
            kept += 1
            if kept >= max_per_section:
                break

    # 2) Fallback from citations if section has no evidence
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
        s.pop("_evidence_seen", None)

    return sections


# =============================================================================
# OpenSearch ingest helpers (AWS-only, review-scoped)
# =============================================================================
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
    """
    Preferred: extract/<doc_id>/raw_text.txt
    Fallback (self-heal): review_pdfs/<doc_id>.pdf -> extract text -> write extract/<doc_id>/ artifacts
    """
    doc_id = (doc_id or "").strip()
    if not doc_id:
        return ""

    extract_key = f"extract/{doc_id}/raw_text.txt"

    # 1) Preferred: already extracted
    try:
        b = storage.get_object(key=extract_key)
        if isinstance(b, (bytes, bytearray)):
            t = b.decode("utf-8", errors="ignore").strip()
            if t:
                return t
    except Exception:
        pass

    # 2) Fallback: derive from PDF
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

    # 3) Persist artifacts (best-effort)
    try:
        raw_text_bytes = text.encode("utf-8", errors="ignore")
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

        # embeddings
        texts = [c["chunk_text"] for c in chunks]
        if not hasattr(llm, "embed_texts"):
            raise RuntimeError("LLM provider does not implement embed_texts() required for vector ingest")
        embeddings = llm.embed_texts(texts)

        if not isinstance(embeddings, list) or len(embeddings) != len(chunks):
            raise RuntimeError(f"embed_texts returned {len(embeddings) if isinstance(embeddings, list) else 'non-list'} embeddings; expected {len(chunks)}")

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

        # Replace semantics per doc_id
        vector.delete_by_document(str(doc_id))
        vector.upsert_chunks(document_id=str(doc_id), chunks=upsert_payload)

        ingested_docs += 1
        ingested_chunks += len(upsert_payload)

    return {"ingested_docs": ingested_docs, "ingested_chunks": ingested_chunks, "skipped_docs": skipped_docs}


# =============================================================================
# LLM call helpers (robust / duck-typed)
# =============================================================================
def _llm_text(llm: Any, prompt: str) -> str:
    """
    Minimal, robust bridge across providers:
      - BedrockLLMProvider.generate(prompt=...)
      - BedrockLLMProvider.complete(prompt=...)
      - Ollama style: generate(prompt=...)
      - fallback: call llm(prompt) if callable

    Returns empty string if no supported method exists.
    """
    if not prompt:
        return ""

    # Common patterns we’ve used across CSS
    for fn_name in ("generate", "complete", "generate_text", "chat"):
        fn = getattr(llm, fn_name, None)
        if callable(fn):
            try:
                out = fn(prompt)  # type: ignore[arg-type]
                if isinstance(out, dict):
                    # common: {"text": "..."}
                    txt = out.get("text")
                    return (txt or "").strip()
                if isinstance(out, str):
                    return out.strip()
            except TypeError:
                # Some providers require named arg
                try:
                    out = fn(prompt=prompt)  # type: ignore[call-arg]
                    if isinstance(out, dict):
                        return str(out.get("text") or "").strip()
                    if isinstance(out, str):
                        return out.strip()
                except Exception:
                    continue
            except Exception:
                continue

    # Callable fallback
    try:
        if callable(llm):
            out = llm(prompt)
            if isinstance(out, dict):
                return str(out.get("text") or "").strip()
            if isinstance(out, str):
                return out.strip()
    except Exception:
        pass

    return ""


def _build_review_summary_prompt(*, intent: str, context_profile: str, context: str, section_headers: List[str]) -> str:
    """
    Keep it deterministic and compatible with your parser:
    - output MUST include the headers verbatim
    - do not add extra headers
    """
    headers = "\n".join(section_headers)

    return (
        "You are Contract Security Studio.\n"
        "Produce a structured solicitation review summary.\n\n"
        "RULES:\n"
        f"- Output MUST include these section headers EXACTLY, each on its own line:\n{headers}\n"
        "- Under each header, write short bullets.\n"
        "- Do NOT invent facts. Use only the provided CONTEXT.\n"
        "- If insufficient, write: Insufficient evidence retrieved for this section.\n"
        "- Do NOT include an 'EVIDENCE:' subsection; evidence is attached separately.\n\n"
        f"MODE:\n- intent={intent}\n- context_profile={context_profile}\n\n"
        "CONTEXT:\n"
        "----------------\n"
        f"{context}\n"
        "----------------\n"
    )


# =============================================================================
# Main entry
# =============================================================================
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
    """
    Robust RAG entrypoint.

    Key behaviors:
    - For force_reingest: ingests per-doc chunks into OpenSearch using:
        extract/<doc_id>/raw_text.txt
      with fallback self-heal from review_pdfs/<doc_id>.pdf.
    - Retrieves with filters={"review_id": <review_id>}.
    - Calls LLM for a structured sectioned summary (headers must match).
    - Parses sections and attaches deterministic evidence.
    """
    def _s(v: Any, max_len: int = 220) -> str:
        try:
            if v is None:
                return ""
            out = str(v).strip()
            if max_len > 0 and len(out) > max_len:
                out = out[:max_len]
            return out
        except Exception:
            return ""

    t0 = time.time() if _timing_enabled() else 0.0
    m = _canonical_mode(mode)

    # Guardrail: avoid expensive re-ingest loops during fast mode unless explicitly allowed.
    if _fast_enabled() and force_reingest and (_env("RAG_ALLOW_FORCE_REINGEST", "0").strip() != "1"):
        print("[RAG] WARN: force_reingest requested but skipped because RAG_FAST=1 and RAG_ALLOW_FORCE_REINGEST!=1")
        force_reingest = False

    intent = (analysis_intent or "strict_summary").strip().lower()
    profile = (context_profile or "fast").strip().lower()

    if _timing_enabled():
        print("[RAG] analyze start", review_id, f"mode={m} intent={intent} profile={profile}")

    # -----------------------------------------------------------------
    # Review lookup:
    # - Prefer DynamoMeta for real cluster
    # - Fallback to legacy storage file readers for local/unit tests
    # -----------------------------------------------------------------
    review: Dict[str, Any] = {}
    docs: List[Dict[str, Any]] = []

    # A) DynamoMeta
    try:
        meta = DynamoMeta()
        detail = meta.get_review_detail(review_id) or {}
        if isinstance(detail, dict) and detail:
            review = detail
            docs = detail.get("docs") or []
    except Exception:
        pass

    # B) Legacy helper
    if not review:
        try:
            candidate = _read_reviews_file(storage, review_id)
        except Exception:
            candidate = None
        try:
            if isinstance(candidate, dict) and candidate.get("id") == review_id:
                review = candidate
            elif isinstance(candidate, list):
                for r in candidate:
                    if isinstance(r, dict) and r.get("id") == review_id:
                        review = r
                        break
        except Exception:
            review = {}

    # C) Test harness fallbacks (FakeStorage)
    if not review:
        for attr in ("_reviews", "reviews"):
            try:
                v = getattr(storage, attr, None)
                if isinstance(v, list):
                    for r in v:
                        if isinstance(r, dict) and r.get("id") == review_id:
                            review = r
                            break
            except Exception:
                pass

    if not isinstance(review, dict):
        review = {}

    if not docs:
        try:
            docs = review.get("docs") or []
        except Exception:
            docs = []

    # -----------------------------------------------------------------
    # force_reingest ingest into OpenSearch
    # -----------------------------------------------------------------
    ingest_stats: Optional[Dict[str, Any]] = None
    if force_reingest:
        try:
            ingest_stats = _ingest_review_into_vectorstore(
                storage=storage,
                llm=llm,
                vector=vector,
                docs=docs,
                review_id=review_id,
                profile=profile,
            )
            print("[RAG] ingest_stats", ingest_stats)
        except Exception as e:
            print("[RAG] ERROR: force_reingest ingest failed:", repr(e))
            ingest_stats = {"error": repr(e)}

    # -----------------------------------------------------------------
    # Retrieval + context
    # -----------------------------------------------------------------
    section_question_map = _question_section_map(intent)
    questions = [q for (_sec, q) in (section_question_map or [])]
    effective_top_k = _effective_top_k(top_k, profile)

    retrieved: Dict[str, List[Dict[str, Any]]] = {}
    context: str = ""
    max_chars: int = 0

    t_ret0 = time.time() if _timing_enabled() else 0.0
    try:
        retrieved, context, max_chars, _signals_from_retrieve = retrieve_context(
            vector=vector,
            llm=llm,
            questions=questions,
            effective_top_k=effective_top_k,
            filters={"review_id": str(review_id)},
            snippet_cap=_effective_snippet_chars(profile),
            intent=intent,
            profile=profile,
            env_get_fn=_env,
            effective_context_chars_fn=_effective_context_chars,
            heuristic_hits=heuristic_hits,
        )
    except Exception as e:
        if debug:
            print("[RAG] retrieve_context failed:", repr(e))
        retrieved, context, max_chars = {}, "", 0

    retrieved_counts: Dict[str, int] = {}
    try:
        for q, hits in (retrieved or {}).items():
            retrieved_counts[str(q)] = len(hits or [])
    except Exception:
        retrieved_counts = {}

    if _timing_enabled():
        print("[RAG] retrieval done", round(time.time() - t_ret0, 3), "s")

    # -----------------------------------------------------------------
    # LLM summary (sectioned)
    # -----------------------------------------------------------------
    prompt = _build_review_summary_prompt(
        intent=intent,
        context_profile=profile,
        context=context,
        section_headers=RAG_REVIEW_SUMMARY_SECTIONS,
    )
    llm_text = _llm_text(llm, prompt)
    summary = _postprocess_review_summary(llm_text or "")

    # -----------------------------------------------------------------
    # Parse sections + attach evidence deterministically
    # -----------------------------------------------------------------
    sections = _parse_review_summary_sections(summary)
    sections = _attach_evidence_to_sections(
        sections,
        section_question_map=section_question_map,
        citations=[],
        retrieved=retrieved,
    )

    # Normalize section outputs (findings/evidence clamp)
    for s in sections:
        _normalize_section_outputs(s)

    # -----------------------------------------------------------------
    # Deterministic risks (minimal, keep your existing shape)
    # -----------------------------------------------------------------
    risks: List[Dict[str, Any]] = []
    if intent == "risk_triage":
        # Prefer autoFlags.hits if present in review payload
        try:
            af = (review or {}).get("autoFlags") or {}
            hits = af.get("hits") or []
            if isinstance(hits, list) and hits:
                for i, h in enumerate(hits):
                    if not isinstance(h, dict):
                        continue
                    lbl = _s(h.get("label") or h.get("name") or h.get("id") or "", 200)
                    if not lbl:
                        continue
                    rid = _s(h.get("hit_key") or h.get("key") or h.get("id") or f"autoflag:{lbl}:{i}", 240)
                    sev = _s(h.get("severity") or "", 40) or "High"
                    risks.append({"id": rid, "label": lbl, "severity": sev, "source": "autoFlag"})
        except Exception:
            pass

    # -----------------------------------------------------------------
    # stats/debug
    # -----------------------------------------------------------------
    stats: Dict[str, Any] = {
        "top_k_requested": int(top_k),
        "top_k_effective": int(effective_top_k),
        "max_context_chars": int(max_chars) if isinstance(max_chars, int) else None,
        "retrieved_counts": retrieved_counts,
    }
    if ingest_stats is not None:
        stats["ingest"] = ingest_stats

    if debug:
        stats["debug_context"] = context
        stats["debug_review_keys"] = sorted(list((review or {}).keys()))
        stats["debug_docs_len"] = len(docs or [])
        stats["debug_llm_text_len"] = len(llm_text or "")

    result: Dict[str, Any] = {
        "review_id": review_id,
        "mode": m,
        "analysis_intent": intent,
        "context_profile": profile,
        "summary": summary,
        "sections": sections,
        "citations": [],
        "retrieved_counts": retrieved_counts,
        "risks": risks,
        "warnings": [],
        "stats": stats if debug else {"top_k_effective": int(effective_top_k)},
    }

    def _owner_for_section(section_id: str) -> str:
    """
    Back-compat export for rag.router import.
    Keep logic deterministic.
    """
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
    if _timing_enabled():
        print("[RAG] analyze done", round(time.time() - t0, 3), "s")

    return result