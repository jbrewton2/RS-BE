# backend/flags/service.py
from __future__ import annotations

from typing import List, Dict
import re

from backend.flags_store import FlagsPayload, FlagRule, load_flags
from backend.flags_usage_store import increment_usage_for_flags


# ---------------------------------------------------------------------------
# Pattern sanitization helpers
# ---------------------------------------------------------------------------

# Characters that strongly indicate the user is already writing regex
_REGEX_META_CHARS = set(r".^$*+?{}[]|()\\")


def _is_plain_text_pattern(pattern: str) -> bool:
    """
    Heuristic: True if the pattern does NOT appear to contain regex syntax.

    If the user has put in things like \b, .*, [], (), etc., we treat it as
    regex and do not auto-wrap it.
    """
    p = pattern.strip()
    if not p:
        return False

    for ch in p:
        if ch in _REGEX_META_CHARS:
            return False
    return True


def sanitize_pattern(pattern: str) -> str:
    """
    Auto-sanitize a single flag pattern.

    - If the pattern looks like plain text: escape it and wrap with word
      boundaries, so "RTO" becomes "\\bRTO\\b".
    - If it looks like regex (contains meta chars), return as-is.

    This keeps the UI simple (users type phrases), while allowing power users
    to provide explicit regex.
    """
    p = pattern.strip()
    if not p:
        return p

    # Already looks like regex? Leave it alone.
    if not _is_plain_text_pattern(p):
        return p

    escaped = re.escape(p)
    return rf"\b{escaped}\b"


def sanitize_patterns(patterns: List[str]) -> List[str]:
    """
    Apply sanitize_pattern to a list of patterns, dropping empty entries.
    """
    return [sanitize_pattern(p) for p in patterns if p and p.strip()]


# ---------------------------------------------------------------------------
# Flag scanning
# ---------------------------------------------------------------------------


def scan_text_for_flags(
    text: str,
    record_usage: bool = True,
) -> Dict[str, object]:
    """
    Canonical flag-scanning function used by /flags/test and (optionally)
    by the reviews backend.

    - Loads FlagsPayload from flags.json via load_flags().
    - Applies all enabled clause/context rules.
    - Returns hits + summary.
    - If record_usage=True, increments usage for each unique flag id that fired.
    """
    text = (text or "").strip()
    flags_payload: FlagsPayload = load_flags()

    hits: List[dict] = []

    def process_rule(rule: FlagRule, group_name: str) -> None:
        rule_id = rule.id
        label = rule.label
        severity = rule.severity or "Medium"
        category = rule.category
        scope_hint = rule.scopeHint
        patterns = rule.patterns or []

        for pattern in patterns:
            try:
                regex = re.compile(pattern, flags=re.IGNORECASE)
                for match in regex.finditer(text):
                    start = match.start()
                    line_num = text[:start].count("\n") + 1
                    hits.append(
                        {
                            "id": rule_id,
                            "label": label,
                            "group": group_name,
                            "severity": severity,
                            "category": category,
                            "scopeHint": scope_hint,
                            "line": line_num,
                            "match": match.group(0),
                        }
                    )
            except re.error:
                # Fallback: simple substring search when pattern is not valid regex
                idx = text.lower().find(pattern.lower())
                if idx != -1:
                    line_num = text[:idx].count("\n") + 1
                    hits.append(
                        {
                            "id": rule_id,
                            "label": label,
                            "group": group_name,
                            "severity": severity,
                            "category": category,
                            "scopeHint": scope_hint,
                            "line": line_num,
                            "match": pattern,
                        }
                    )

    for group_name in ("clause", "context"):
        rules = getattr(flags_payload, group_name, []) or []
        for rule in rules:
            if rule.enabled is False:
                continue
            process_rule(rule, group_name)

    # Build summary
    severity_order = ["Critical", "High", "Medium", "Low"]
    counts_by_group: Dict[str, int] = {"clause": 0, "context": 0}
    counts_by_severity: Dict[str, int] = {s: 0 for s in severity_order}
    counts_by_category: Dict[str, int] = {}

    for h in hits:
        g = h.get("group") or "clause"
        s = h.get("severity") or "Medium"
        c = h.get("category") or "OTHER"

        counts_by_group[g] = counts_by_group.get(g, 0) + 1
        if s not in counts_by_severity:
            counts_by_severity[s] = 0
        counts_by_severity[s] += 1
        counts_by_category[c] = counts_by_category.get(c, 0) + 1

    max_severity = None
    for sev in severity_order:
        if counts_by_severity.get(sev, 0) > 0:
            max_severity = sev
            break

    summary = {
        "total": len(hits),
        "byGroup": counts_by_group,
        "bySeverity": counts_by_severity,
        "byCategory": counts_by_category,
        "maxSeverity": max_severity,
    }

    if record_usage:
        unique_flag_ids = sorted({h.get("id") for h in hits if h.get("id")})
        if unique_flag_ids:
            increment_usage_for_flags(unique_flag_ids)

    return {"hits": hits, "summary": summary}
