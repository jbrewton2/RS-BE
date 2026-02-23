from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# NOTE: This module is intentionally "pure-ish":
# - No provider resolution
# - No vector/llm calls
# - Only section parsing/normalization and evidence attachment


_SECTION_MAX_FINDINGS = 9


def _canon_header_line(raw: str) -> str:
    s = (raw or "").strip()
    s = s.strip("*").strip()
    s = s.replace(":", "").strip()
    s = " ".join(s.split())
    return s.upper()


def _is_section_header_line(raw: str) -> Optional[str]:
    cand = _canon_header_line(raw)
    if not cand:
        return None

    # Heuristic: short all-caps-ish header tokens
    if len(cand) > 120:
        return None
    return cand


def _split_sections(text: str) -> Dict[str, str]:
    lines = (text or "").splitlines()
    out: Dict[str, List[str]] = {}
    cur: Optional[str] = None

    for line in lines:
        canon = _is_section_header_line(line)
        if canon:
            cur = canon
            out.setdefault(cur, [])
            continue
        if cur is None:
            continue
        out[cur].append(line)

    return {k: "\n".join(v).strip() for k, v in out.items()}


def _render_sections_in_order(sections: Dict[str, str], order: List[str]) -> str:
    parts: List[str] = []
    for h in (order or []):
        canon = _canon_header_line(h)
        body = (sections or {}).get(canon, "").strip()
        parts.append(canon)
        parts.append(body if body else "Insufficient evidence retrieved for this section.")
    return "\n".join(parts).strip()


def _postprocess_review_summary(text: str) -> str:
    # Keep it conservative; service.py handles token clamp + mojibake guard.
    t = (text or "").strip()
    return t


def _strip_owner_tokens(s: str) -> str:
    # Removes inline "Owner:" tokens if the model tries to include them.
    t = (s or "").replace("\r", " ")
    t = "\n".join([ln for ln in t.splitlines() if not ln.strip().lower().startswith("owner:")])
    return t.strip()


def _normalize_bullet_text(t: str) -> str:
    s = (t or "").replace("\r", " ").strip()
    # normalize common mojibake-ish ellipsis etc.
    s = s.replace("…", "...")
    return s


def _clean_findings_line(s: str) -> Optional[str]:
    t = (s or "").strip()
    if not t:
        return None
    t = t.lstrip("-•*").strip()
    t = _normalize_bullet_text(t)
    return t if t else None


def _normalize_section_outputs(section: Dict[str, Any], *, max_findings: int = _SECTION_MAX_FINDINGS) -> None:
    if not isinstance(section, dict):
        return

    # Normalize text field
    txt = section.get("text") or ""
    txt = _strip_owner_tokens(str(txt))
    section["text"] = txt

    # Normalize findings list if present
    f = section.get("findings")
    if isinstance(f, list):
        cleaned: List[str] = []
        seen = set()
        for item in f:
            cl = _clean_findings_line(str(item))
            if not cl:
                continue
            key = cl.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(cl)
            if max_findings and len(cleaned) >= max_findings:
                break
        section["findings"] = cleaned


def _slug(s: str) -> str:
    raw = (s or "").strip().lower()
    out = []
    for ch in raw:
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return ("".join(out).strip("-"))[:80] or "section"


def _parse_review_summary_sections(text: str) -> List[Dict[str, Any]]:
    # Parses a flat "HEADER\ntext\nHEADER\ntext" into [{id,title,text},...]
    chunks = _split_sections(text or "")
    out: List[Dict[str, Any]] = []
    for header, body in (chunks or {}).items():
        hid = _slug(header)
        out.append(
            {
                "id": hid,
                "title": header,
                "text": (body or "").strip(),
                "owner": "",
                "findings": [],
                "evidence": [],
            }
        )
    return out


def _evidence_key(ev: Dict[str, Any]) -> str:
    try:
        doc = str(((ev or {}).get("doc_name") or (ev or {}).get("doc") or "")).strip()
        cs = str(((ev or {}).get("char_start") or "")).strip()
        ce = str(((ev or {}).get("char_end") or "")).strip()
        return f"{doc}|{cs}|{ce}"
    except Exception:
        return ""


def _attach_evidence_to_sections(
    sections: List[Dict[str, Any]],
    *,
    section_question_map: List[Tuple[str, str]],
    citations: List[Dict[str, Any]],
    retrieved: Dict[str, List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    # Minimal deterministic mapping: question->section id via section_question_map
    # retrieved is keyed by question text (as used in service.py)
    sec_by_id = {str(s.get("id") or ""): s for s in (sections or []) if isinstance(s, dict)}

    for sec_id, q in (section_question_map or []):
        sid = str(sec_id or "").strip()
        if not sid or sid not in sec_by_id:
            continue
        hits = (retrieved or {}).get(q) or []
        if not isinstance(hits, list) or not hits:
            continue

        ev_list = sec_by_id[sid].get("evidence")
        if not isinstance(ev_list, list):
            ev_list = []
            sec_by_id[sid]["evidence"] = ev_list

        seen = set([_evidence_key(e) for e in ev_list if isinstance(e, dict)])
        for h in hits:
            if not isinstance(h, dict):
                continue
            meta = h.get("meta") or {}
            ev = {
                "doc_name": meta.get("doc_name") or meta.get("doc") or "",
                "doc_id": meta.get("doc_id") or "",
                "char_start": meta.get("char_start"),
                "char_end": meta.get("char_end"),
                "score": h.get("score"),
            }
            k = _evidence_key(ev)
            if not k or k in seen:
                continue
            seen.add(k)
            ev_list.append(ev)

    return sections


def _strengthen_overview_from_evidence(sections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Deterministically strengthen the OVERVIEW section from attached evidence.

    Test expectation:
      - If OVERVIEW has evidence and no findings, create at least one finding.
    """
    try:
        if not isinstance(sections, list):
            return sections

        ov = None
        for s in sections:
            if isinstance(s, dict) and str(s.get("id") or "").strip().lower() == "overview":
                ov = s
                break
        if not isinstance(ov, dict):
            return sections

        findings = ov.get("findings")
        if not isinstance(findings, list):
            findings = []
            ov["findings"] = findings

        # Only add if empty to avoid duplicating existing model findings
        if findings:
            return sections

        ev = ov.get("evidence")
        if not isinstance(ev, list) or not ev:
            return sections

        seen = set()
        added = 0
        for e in ev:
            if not isinstance(e, dict):
                continue
            txt = str(
                e.get("text")
                or e.get("chunk_text")
                or e.get("excerpt")
                or ""
            ).strip()
            if not txt:
                continue
            # normalize and cap
            txt = txt.replace("\r", " ").replace("\n", " ").strip()
            if len(txt) > 220:
                txt = txt[:217].rstrip() + "..."
            key = txt.lower()
            if key in seen:
                continue
            seen.add(key)
            findings.append(txt)
            added += 1
            if added >= 3:
                break

        return sections
    except Exception:
        return sections

def _backfill_sections_from_evidence(sections: List[Dict[str, Any]], intent: str = "strict_summary") -> List[Dict[str, Any]]:
    """
    Deterministic backfill behavior used to avoid empty UI sections.

    Test expectations:
      - If section has evidence and empty findings, don't hallucinate findings (overview strengthening handles overview).
      - If section has no evidence, add at least one gap and one recommended action.
    """
    for s in (sections or []):
        if not isinstance(s, dict):
            continue

        # Ensure lists exist for UI contract
        if not isinstance(s.get("gaps"), list):
            s["gaps"] = []
        if not isinstance(s.get("recommended_actions"), list):
            s["recommended_actions"] = []
        if not isinstance(s.get("findings"), list):
            s["findings"] = []

        txt = str(s.get("text") or "").strip()
        ev = s.get("evidence") if isinstance(s.get("evidence"), list) else []

        if not txt and ev:
            s["text"] = "Evidence retrieved. Review evidence items for obligations and constraints."

        if not txt and not ev:
            s["text"] = "Insufficient evidence retrieved for this section."

        # If no evidence, add deterministic gaps/actions
        if not ev:
            gap_msg = "No contract evidence retrieved for this section (retrieval starvation or mapping gap)."
            act_msg = "Action: verify ingestion/indexing for this review and consider increasing top_k or reingesting documents."

            if gap_msg not in s["gaps"]:
                s["gaps"].append(gap_msg)
            if act_msg not in s["recommended_actions"]:
                s["recommended_actions"].append(act_msg)

    return sections

def owner_for_section(section_id: str) -> str:
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


