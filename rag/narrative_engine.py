from __future__ import annotations

from typing import Any, Dict, List, Tuple


def _safe(s: str, max_len: int) -> str:
    t = (s or "").replace("\r", " ").replace("\n", " ").strip()
    if max_len > 0 and len(t) > max_len:
        t = t[: max_len - 3].rstrip() + "..."
    return t


def _render_evidence_lines_for_section(
    *,
    section_id: str,
    section_question_map: List[Tuple[str, str]],
    retrieved: Dict[str, List[Dict[str, Any]]],
    snippet_cap: int,
    max_lines: int,
) -> List[str]:
    out: List[str] = []
    sid = (section_id or "").strip().lower()
    if not sid:
        return out

    for sec_id, q in (section_question_map or []):
        if str(sec_id or "").strip().lower() != sid:
            continue
        hits = (retrieved or {}).get(q) or []
        for h in hits[:6]:
            txt = str(h.get("chunk_text") or "").strip()
            if not txt:
                continue
            if snippet_cap > 0 and len(txt) > snippet_cap:
                txt = txt[:snippet_cap].rstrip() + "..."
            meta = h.get("meta") or {}
            doc = meta.get("doc_name") or h.get("doc_name") or meta.get("doc_id") or h.get("document_id") or "doc"
            out.append(f"- ({doc}) {txt}")
            if len(out) >= int(max_lines):
                return out

    return out


def _section_prompt(*, section_header: str, evidence_lines: List[str], signals: str, max_sentences: int) -> str:
    ev = "\n".join([_safe(x, 260) for x in (evidence_lines or [])]).strip()
    if not ev:
        ev = "(no evidence retrieved)"

    sig = (signals or "").strip()
    if sig:
        sig = _safe(sig, 1200)

    return "\n".join(
        [
            "TASK",
            "Write a short narrative for this section based ONLY on the evidence lines.",
            "",
            "RULES",
            "- Plain text only",
            "- Do NOT fabricate facts",
            "- If insufficient evidence: write 'INSUFFICIENT EVIDENCE'",
            "- Do not cite deterministic signals as contract text",
            "",
            f"SECTION HEADER: {section_header}",
            "",
            "DETERMINISTIC SIGNALS (NOT CONTRACT EVIDENCE):",
            sig if sig else "(none)",
            "",
            "CONTRACT EVIDENCE LINES:",
            ev,
            "",
            "OUTPUT",
            f"Write 2-{int(max_sentences)} sentences.",
        ]
    ).strip()


def generate_summary_multi_pass(
    *,
    llm: Any,
    section_headers: List[str],
    section_ids: List[str],
    section_question_map: List[Tuple[str, str]],
    retrieved: Dict[str, List[Dict[str, Any]]],
    snippet_cap: int,
    signals: str,
    max_evidence_lines: int = 12,
    max_sentences: int = 6,
) -> str:
    """
    Multi-pass narrative generation to stay under hard context limits (e.g., 8192).
    Produces a summary string that matches the existing parser format:
      HEADER
      text...
      HEADER
      text...
    """
    if not hasattr(llm, "generate"):
        return ""

    parts: List[str] = []
    for header, sid in zip(section_headers or [], section_ids or []):
        evid_lines = _render_evidence_lines_for_section(
            section_id=str(sid),
            section_question_map=section_question_map,
            retrieved=retrieved,
            snippet_cap=int(snippet_cap),
            max_lines=int(max_evidence_lines),
        )
        prompt = _section_prompt(
            section_header=str(header),
            evidence_lines=evid_lines,
            signals=signals,
            max_sentences=int(max_sentences),
        )

        try:
            r = llm.generate(prompt)
            txt = str(r.get("text") or "") if isinstance(r, dict) else str(r or "")
        except Exception:
            txt = ""

        txt = (txt or "").strip() or "INSUFFICIENT EVIDENCE"
        parts.append(str(header).strip())
        parts.append(txt)

    return "\n".join(parts).strip()
