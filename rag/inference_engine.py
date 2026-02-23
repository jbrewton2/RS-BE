from __future__ import annotations

from typing import Any, Dict, List, Optional


def _safe_line(s: str, max_len: int = 220) -> str:
    t = (s or "").replace("\r", " ").replace("\n", " ").strip()
    if max_len > 0 and len(t) > max_len:
        t = t[: max_len - 3].rstrip() + "..."
    return t


def _build_inference_prompt_for_section(
    *,
    section_title: str,
    section_text: str,
    evidence_snips: List[str],
    max_candidates: int,
) -> str:
    ev_block = "\n".join([f"- { _safe_line(x, 240) }" for x in (evidence_snips or [])[:6]]).strip()
    if not ev_block:
        ev_block = "- (no evidence snippets available)"

    return "\n".join(
        [
            "TASK",
            "Generate Tier-1 (inference) risk candidates for this section. These are low-confidence hypotheses to investigate.",
            "",
            "RULES",
            "- Do NOT invent facts; phrase as 'May be missing/unclear' when uncertain.",
            "- Keep each candidate <= 160 characters.",
            "- Provide ONLY bullet lines starting with '- '. No extra text.",
            f"- Provide at most {int(max_candidates)} candidates.",
            "",
            f"SECTION: {section_title}",
            "",
            "SECTION TEXT (may be incomplete):",
            _safe_line(section_text, 1200),
            "",
            "EVIDENCE SNIPPETS:",
            ev_block,
            "",
            "OUTPUT",
            "- <candidate 1>",
            "- <candidate 2>",
        ]
    ).strip()


def generate_inference_candidates_multi_pass(
    *,
    llm: Any,
    sections: List[Dict[str, Any]],
    max_candidates_total: int = 20,
    max_candidates_per_section: int = 4,
) -> List[str]:
    """
    Multi-pass inference candidate generator (Tier 1 required).

    Makes small LLM calls per section to stay under context limits (e.g., Bedrock 8192).
    Returns a de-duped list of candidate strings.
    """
    if not hasattr(llm, "generate"):
        return []

    out: List[str] = []
    seen = set()

    for s in (sections or []):
        if not isinstance(s, dict):
            continue
        title = str(s.get("title") or s.get("header") or s.get("id") or "SECTION").strip() or "SECTION"
        text = str(s.get("text") or "").strip()

        # Try to pull small evidence snippets from attached evidence objects
        ev_snips: List[str] = []
        ev = s.get("evidence")
        if isinstance(ev, list):
            for e in ev[:6]:
                if not isinstance(e, dict):
                    continue
                # prefer explicit text if present; else fallback to doc span summary
                t = str(e.get("text") or e.get("chunk_text") or "").strip()
                if not t:
                    doc = str(e.get("doc_name") or e.get("doc") or "").strip()
                    cs = str(e.get("char_start") or e.get("charStart") or "").strip()
                    ce = str(e.get("char_end") or e.get("charEnd") or "").strip()
                    t = f"{doc} span {cs}-{ce}".strip()
                if t:
                    ev_snips.append(t)

        prompt = _build_inference_prompt_for_section(
            section_title=title,
            section_text=text,
            evidence_snips=ev_snips,
            max_candidates=int(max_candidates_per_section),
        )

        try:
            r = llm.generate(prompt)
            txt = ""
            if isinstance(r, dict):
                txt = str(r.get("text") or "")
            else:
                txt = str(r or "")
        except Exception:
            continue

        for line in (txt or "").splitlines():
            line = line.strip()
            if not line.startswith("- "):
                continue
            cand = line[2:].strip()
            cand = _safe_line(cand, 160)
            if not cand:
                continue
            key = cand.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(cand)
            if len(out) >= int(max_candidates_total):
                return out

    return out
