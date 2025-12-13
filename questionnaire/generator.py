from __future__ import annotations

from typing import List


async def generate_question_variants(question_text: str) -> List[str]:
    """
    Generate simple alternate phrasings for a questionnaire question.

    This implementation is deterministic and does not call an LLM. It is
    designed to be ASCII-only to avoid encoding issues.
    """
    base = (question_text or "").strip()
    if not base:
        return []

    # Remove trailing question mark so we can reuse the stem
    if base.endswith("?"):
        base_no_q = base[:-1].strip()
    else:
        base_no_q = base

    # Very simple patterns that keep meaning similar
    variants: List[str] = []

    # Original question
    variants.append(base)

    # Describe / Explain / Please describe / Provide details on ...
    if base_no_q:
        variants.append("Describe " + base_no_q + ".")
        variants.append("Explain " + base_no_q + ".")
        variants.append("Please describe " + base_no_q + ".")
        variants.append("Provide details on " + base_no_q + ".")

    # Deduplicate while preserving order
    seen = set()
    result: List[str] = []
    for v in variants:
        s = v.strip()
        if not s:
            continue
        if s in seen:
            continue
        seen.add(s)
        result.append(s)

    return result
