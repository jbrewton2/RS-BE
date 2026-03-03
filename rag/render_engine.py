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
    include_top_risks: bool = True,
    max_findings: int = 4,
    max_risks: int = 3,
) -> str:
    """
    Deterministically render a coherent "Text" view from structured sections.
    No LLM calls. Safe for both single-pass and multipass pipelines.
    """
    parts: List[str] = []

    for s in (sections or []):
        if not isinstance(s, dict):
            continue

        title = _clean_line(s.get("title") or s.get("id") or "", max_len=80)
        if not title:
            continue

        summary = _clean_line(s.get("text") or "", max_len=260)
        parts.append(title)
        if summary:
            parts.append(summary)

        findings = s.get("findings")
        if isinstance(findings, list) and findings:
            for b in findings[: int(max_findings)]:
                bb = _clean_line(b, max_len=220)
                if bb:
                    parts.append(f"- {bb}")

        if include_top_risks:
            rf = s.get("risk_findings")
            if isinstance(rf, list) and rf:
                parts.append("Top risks:")
                for r in rf[: int(max_risks)]:
                    if not isinstance(r, dict):
                        continue
                    lab = _clean_line(r.get("label") or "", max_len=240)
                    if not lab:
                        continue
                    sev = _clean_line(r.get("severity") or "", max_len=24)
                    tier = _clean_line(r.get("tier") or "", max_len=24)
                    parts.append(f"- [{tier or 'tier?'}|{sev or 'sev?'}] {lab}")

        parts.append("")

    return "\n".join(parts).rstrip() + "\n"
