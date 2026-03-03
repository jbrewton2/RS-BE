from __future__ import annotations

from typing import Any, Dict, List


def _clean_line(s: Any, *, max_len: int = 260) -> str:
    t = str(s or "").replace("\r", " ").replace("\n", " ").strip()
    t = " ".join(t.split())

    # common junk
    low = t.lower()
    if low.startswith("here is the output:"):
        t = t[len("here is the output:") :].strip()

    # strip surrounding quotes
    if len(t) >= 2 and ((t[0] == t[-1] == '"') or (t[0] == t[-1] == "'")):
        t = t[1:-1].strip()

    if len(t) > max_len:
        t = t[:max_len].rstrip() + "..."
    return t


def render_text_summary_from_sections(
    sections: List[Dict[str, Any]],
    *,
    include_top_risks: bool = False,
    max_findings: int = 4,   # unused in risk-first mode, kept for compatibility
    max_risks: int = 3,
) -> str:
    """
    Deterministically render a coherent "Text" view from structured sections.
    Risk-first: bullets are risk labels by section. Evidence snippets stay in Sections view.
    """
    parts: List[str] = []

    for s in (sections or []):
        if not isinstance(s, dict):
            continue

        title = _clean_line(s.get("title") or s.get("id") or "", max_len=80)
        if not title:
            continue

        summary = _clean_line(s.get("text") or "", max_len=220)

        parts.append(title)
        if summary:
            parts.append(summary)

        # Risk-first bullets
        rf = s.get("risk_findings")
        bullets: List[str] = []
        if isinstance(rf, list) and rf:
            for r in rf[: int(max_risks)]:
                if not isinstance(r, dict):
                    continue
                lab = _clean_line(r.get("label") or "", max_len=220)
                if lab:
                    bullets.append(lab)
        else:
            # Fallback: show up to 2 evidence-derived findings (trimmed)
            f = s.get("findings")
            if isinstance(f, list) and f:
                for b in f[:2]:
                    bb = _clean_line(b, max_len=180)
                    if summary and (bb == summary or bb.startswith(summary[:60])):
                        continue
                    if bb:
                        bullets.append(bb)

        if bullets:
            parts.append("")
            for b in bullets:
                parts.append(f"- {b}")

        parts.append("")  # spacer

    return "\n".join(parts).rstrip() + "\n"
