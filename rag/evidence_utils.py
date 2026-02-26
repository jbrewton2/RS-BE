from __future__ import annotations

import re
from typing import Optional

# -----------------------------------------------------------------------------
# Evidence scoring / glossary / compliance regexes (single source of truth)
# -----------------------------------------------------------------------------
_GLOSSARY_RE   = re.compile(r"\b(glossary|definitions?)\b", re.IGNORECASE)
_SIGNAL_RE     = re.compile(r"\b(shall|must|required|will|will not|shall not)\b", re.IGNORECASE)
_COMPLIANCE_RE = re.compile(r"\b(NIST|RMF|CMMC|FedRAMP|DFARS|FAR|ITAR|HIPAA|SOX|PCI|CJIS|800-53|800-171)\b", re.IGNORECASE)

def _is_glossary_text(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if "GLOSSARY" in t.upper() or "DEFINITIONS" in t.upper():
        return True
    return bool(_GLOSSARY_RE.search(t))

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



    def add_ev(sec: Dict[str, Any], ev: Dict[str, Any]) -> None:
        key = _evidence_key(ev)
        if key in sec["_seen"]:
            return
        sec["_seen"].add(key)
        sec["evidence"].append(ev)

    for sec_title, q in section_question_map:
        sec = sec_by_title.get(sec_title)
        if not sec:
            continue
        hits = retrieved.get(q) or []
        kept = 0
        for h in hits:
            chunk_text = (h.get("chunk_text") or "").strip()
            if not chunk_text:
                continue
            meta = h.get("meta") or {}
            ev = {
                "docId": meta.get("doc_id") or h.get("document_id"),
                "doc": meta.get("doc_name") or h.get("doc_name") or meta.get("doc_id"),
                "text": _extract_obligation_excerpt(chunk_text, max_len=1200),
                "charStart": meta.get("char_start"),
                "charEnd": meta.get("char_end"),
                "page_number": meta.get("page_number") or meta.get("page") or meta.get("pageNumber"),
                "score": h.get("score"),
            }
            add_ev(sec, ev)
            kept += 1
            if kept >= max_per_section:
                break

    for s in sections:
        s.pop("_seen", None)
    return sections


# =============================================================================
# Section backfill helpers (tests expect gaps on empty sections)
# =============================================================================
def _strengthen_overview_from_evidence(sections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """If OVERVIEW is empty/insufficient, deterministically strengthen it from the highest-signal evidence across all sections."""
    if not isinstance(sections, list) or not sections:
        return sections

    overview = None
    for s in sections:
        if not isinstance(s, dict):
            continue
        if (s.get("id") or "").strip().lower() == "overview" or (s.get("title") or "").strip() == "OVERVIEW":
            overview = s
            break
    if overview is None:
        return sections

    findings = overview.get("findings") or []
    if not isinstance(findings, list):
        findings = []

    has_real = any((str(x or "").strip() and INSUFFICIENT.lower() not in str(x).lower()) for x in findings)
    if has_real:
        return sections

    ev_all: List[Dict[str, Any]] = []
    for s in sections:
        if not isinstance(s, dict):
            continue
        evs = s.get("evidence") or []
        if isinstance(evs, list):
            for ev in evs:
                if isinstance(ev, dict) and (ev.get("text") or "").strip():
                    ev_all.append(ev)

    if not ev_all:
        return sections
