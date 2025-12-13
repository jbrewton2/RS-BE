# backend/flags/router.py
from __future__ import annotations

import csv
import io
import uuid
from typing import List, Optional, Dict

from fastapi import APIRouter, HTTPException, UploadFile, File
from pydantic import BaseModel

from backend.flags_store import (
    FlagRule,
    FlagsPayload,
    load_flags,
    save_flags,
)
from backend.flags_usage_store import get_usage_map
from backend.flags.service import scan_text_for_flags, sanitize_patterns
from backend.reviews.router import _read_reviews_file

router = APIRouter(
    prefix="/flags",
    tags=["flags"],
)


# ---------------------------------------------------------------------
# Internal helper: sanitize all patterns in FlagsPayload
# ---------------------------------------------------------------------
def _sanitize_flags_payload(payload: FlagsPayload) -> FlagsPayload:
    """
    Walk the FlagsPayload and sanitize all rule.patterns.

    This is called whenever we persist flags (PUT /flags, import-csv) so that
    plain-text patterns are converted into safe regex with word boundaries.
    """
    for group_name in ("clause", "context"):
        rules: List[FlagRule] = getattr(payload, group_name, []) or []
        for rule in rules:
            if rule.patterns:
                # Convert to list, sanitize, and reassign
                rule.patterns = sanitize_patterns(list(rule.patterns))
    return payload


# ---------------------------------------------------------------------
# GET /flags
# ---------------------------------------------------------------------
@router.get("", response_model=FlagsPayload)
async def get_flags():
    """
    Return the current clause/context flag rules from flags.json.
    """
    try:
        return load_flags()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to load flags: {exc}",
        )


# ---------------------------------------------------------------------
# PUT /flags
# ---------------------------------------------------------------------
@router.put("", response_model=FlagsPayload)
async def update_flags(payload: FlagsPayload):
    """
    Replace the entire flags.json payload with the provided clauses/context.
    The frontend (FlagManager) already sends the right shape.

    We auto-sanitize patterns here so that simple phrases like "RTO" or
    "business continuity" become regexes with word boundaries.
    """
    try:
        cleaned = _sanitize_flags_payload(payload)
        save_flags(cleaned)
        return cleaned
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to save flags: {exc}",
        )


# ---------------------------------------------------------------------
# POST /flags/test  (auto-flagging with summary + usage tracking)
# ---------------------------------------------------------------------
@router.post("/test")
async def test_flags(payload: Dict[str, object]):
    """
    Test the flag rules against arbitrary text.
    """
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(
            status_code=400,
            detail="Payload must include non-empty 'text'.",
        )

    record_usage = bool(payload.get("record_usage", True))

    result = scan_text_for_flags(text, record_usage=record_usage)
    return result


# ---------------------------------------------------------------------
# POST /flags/import-csv  (import flags from spreadsheet)
# ---------------------------------------------------------------------
@router.post("/import-csv", response_model=FlagsPayload)
async def import_flags_from_csv(file: UploadFile = File(...)):
    """
    Import flag rules from a CSV file into the current flags set.

    Patterns loaded from CSV are also auto-sanitized (word-boundary regex)
    before being stored.
    """
    try:
        data = await file.read()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to read CSV: {exc}")

    if not data:
        raise HTTPException(status_code=400, detail="Empty CSV file.")

    text = data.decode("utf-8", errors="ignore")
    reader = csv.DictReader(io.StringIO(text))

    flags_payload = load_flags()

    for row in reader:
        group_raw = (row.get("group") or "clause").strip().lower()
        label = (row.get("label") or "").strip()
        if not label:
            continue

        patterns_raw = row.get("patterns") or ""
        raw_patterns = [p.strip() for p in patterns_raw.split("|") if p.strip()]
        patterns = sanitize_patterns(raw_patterns)

        tip = (row.get("tip") or "").strip()
        severity = (row.get("severity") or "Medium").strip().title()
        category = (row.get("category") or None) or None
        scope_hint = (row.get("scopeHint") or None) or None

        if severity not in ("Critical", "High", "Medium", "Low"):
            severity = "Medium"

        group = "clause" if group_raw != "context" else "context"

        new_rule = FlagRule(
            id=str(uuid.uuid4()),
            group=group,  # type: ignore
            label=label,
            patterns=patterns,
            tip=tip,
            severity=severity,  # type: ignore
            enabled=True,
            category=category,
            scopeHint=scope_hint,  # type: ignore
            examples=None,
        )

        if group == "clause":
            flags_payload.clause.append(new_rule)
        else:
            flags_payload.context.append(new_rule)

    flags_payload = _sanitize_flags_payload(flags_payload)
    save_flags(flags_payload)
    return flags_payload


# ---------------------------------------------------------------------
# GET /flags/usage  (per-flag usage counts)
# ---------------------------------------------------------------------
@router.get("/usage")
async def get_flags_usage():
    """
    Return usage counts per flag id.
    """
    return get_usage_map()


# ---------------------------------------------------------------------
# POST /flags/explain  (deterministic "why this fired" explanation)
# ---------------------------------------------------------------------


class FlagExplainRequest(BaseModel):
    """
    Request to explain a specific auto flag hit on a review.

    Primary selector:
    - hit_key: stable identifier for a hit (e.g., "<docId>:<flagId>:<line>:<index>")

    Backwards-compatible selectors:
    - hit_index: index into review.autoFlags.hits
    - doc_id + flag_id (+ optional hit_index within that subset)
    - snippet: optional, used to override/anchor the snippet used in explanation
    """
    review_id: str
    hit_key: Optional[str] = None
    hit_index: Optional[int] = None
    doc_id: Optional[str] = None
    flag_id: Optional[str] = None
    snippet: Optional[str] = None


class FlagExplainResponse(BaseModel):
    """
    Response containing a deterministic explanation for a flag hit.

    - explanation: single short paragraph explaining why the flag fired and
      what the reviewer should verify.
    - flaggedText: snippet of text that was used as the basis for the explanation.
    - reasoning: reserved for future use if we add more structured reasoning.
    """
    explanation: str
    flaggedText: Optional[str] = None
    reasoning: Optional[str] = None


def _select_hit(
    hits: List[dict],
    body: FlagExplainRequest,
) -> dict:
    """
    Select a single hit from review.autoFlags.hits using a deterministic order:

    1. hit_key (matches 'hit_key' or 'hitKey' on the hit)
    2. doc_id + flag_id (+ optional hit_index within that subset)
    3. global hit_index
    4. fallback: first hit
    """
    if not hits:
        raise HTTPException(status_code=404, detail="No flag hits found for this review.")

    # 1) hit_key (preferred)
    if body.hit_key:
        for h in hits:
            if h.get("hit_key") == body.hit_key or h.get("hitKey") == body.hit_key:
                return h

    # 2) doc_id + flag_id filter
    subset = hits
    if body.doc_id:
        subset = [h for h in subset if h.get("docId") == body.doc_id]
    if body.flag_id:
        subset = [h for h in subset if h.get("id") == body.flag_id]

    if subset:
        if body.hit_index is not None and 0 <= body.hit_index < len(subset):
            return subset[body.hit_index]
        return subset[0]

    # 3) global hit_index
    if body.hit_index is not None and 0 <= body.hit_index < len(hits):
        return hits[body.hit_index]

    # 4) fallback
    return hits[0]


def _extract_snippet_from_doc(review: dict, hit: dict, window: int = 3) -> str:
    """
    Reconstruct a snippet around the hit's line from the associated document.
    """
    doc_id = hit.get("docId")
    line_num = hit.get("line") or 0

    docs = review.get("docs") or []
    doc = next((d for d in docs if d.get("id") == doc_id), None)
    full_text = (doc.get("text") or doc.get("content") or "") if doc else ""
    if not full_text:
        return ""

    lines = full_text.splitlines()
    try:
        idx = int(line_num)
    except Exception:
        idx = 0

    idx = max(0, min(idx, len(lines) - 1))
    start = max(0, idx - window)
    end = min(len(lines), idx + window + 1)

    snippet_lines = [ln.strip() for ln in lines[start:end] if ln.strip()]
    return " ".join(snippet_lines).strip()


def _find_flag_rule(flags_payload: FlagsPayload, flag_id: str) -> Optional[FlagRule]:
    """
    Locate the FlagRule for a given flag_id across clause + context buckets.
    """
    for rule in (flags_payload.clause or []):
        if rule.id == flag_id:
            return rule
    for rule in (flags_payload.context or []):
        if rule.id == flag_id:
            return rule
    return None


def _compose_flag_explanation(
    rule: FlagRule,
    hit: dict,
    doc_name: str,
    snippet: str,
) -> str:
    """
    Build a deterministic explanation string from rule metadata + hit + snippet.

    Format (single paragraph):

    The "<Label>" flag was triggered because <reason about snippet>. The relevant
    text in "<doc_name>" around line <line> says: "<snippet>". It is marked as
    <severity> severity in the <category> category under the <group> group.
    As the reviewer, you should: <tip>.
    """
    label = rule.label or rule.id
    severity = (rule.severity or "Medium").lower()
    category = rule.category or ""
    group = hit.get("group") or rule.group or ""
    scope_hint = rule.scopeHint or ""
    tip = (rule.tip or "").strip()

    line_num = hit.get("line")
    line_str = f" around line {line_num}" if line_num is not None else ""

    # Rough "why" sentence based on scope/category
    reason_parts = []
    if scope_hint:
        reason_parts.append(scope_hint)
    if category:
        reason_parts.append(category)
    reason_desc = ", ".join(p for p in reason_parts if p)

    base_reason = (
        f'The "{label}" flag was triggered because the contract language '
        f"indicates {reason_desc or 'requirements related to this control area'}."
    )

    snippet_part = ""
    if snippet:
        snippet_part = f' The relevant text in "{doc_name}"{line_str} says: "{snippet}".'

    sev_part = f" It is marked as {severity} severity" if severity else ""
    cat_part = f" in the {category} category" if category else ""
    group_part = f" under the {group} group" if group else ""
    meta_parts = (sev_part + cat_part + group_part).rstrip(".")
    meta_part = f"{meta_parts}." if meta_parts else ""

    guidance = ""
    if tip:
        guidance = f" As the reviewer, you should: {tip}"

    return (base_reason + snippet_part + meta_part + guidance).strip()


@router.post("/explain", response_model=FlagExplainResponse)
async def explain_flag_hit(body: FlagExplainRequest):
    """
    Explain why a given flag hit fired, using a deterministic builder.

    - No LLM is called here, which avoids hallucinations and prompt drift.
    - We rely on the stored flag rule metadata + the actual text snippet.
    """
    # 1) Locate review
    reviews = _read_reviews_file()
    review = next((r for r in reviews if r.get("id") == body.review_id), None)
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")

    auto_flags = review.get("autoFlags") or {}
    hits = auto_flags.get("hits") or []
    if not isinstance(hits, list) or not hits:
        raise HTTPException(status_code=404, detail="No auto flag hits for this review")

    # 2) Select hit deterministically
    hit = _select_hit(hits, body)

    flag_id = hit.get("id")
    if not flag_id:
        raise HTTPException(status_code=400, detail="Hit has no flag id")

    doc_id = hit.get("docId")

    # 3) Look up flag rule
    flags_payload = load_flags()
    rule = _find_flag_rule(flags_payload, flag_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Flag rule not found")

    # 4) Determine snippet
    snippet = (
        (body.snippet or "").strip()
        or (hit.get("snippet") or "").strip()
        or _extract_snippet_from_doc(review, hit)
    )

    # 5) Doc name for message
    docs = review.get("docs") or []
    doc = next((d for d in docs if d.get("id") == doc_id), None)
    doc_name = (
        (doc.get("name") or doc.get("filename") or "this document")
        if doc
        else "this review"
    )

    # 6) Compose deterministic explanation
    explanation = _compose_flag_explanation(
        rule=rule,
        hit=hit,
        doc_name=doc_name,
        snippet=snippet or "",
    )

    return FlagExplainResponse(
        explanation=explanation,
        flaggedText=snippet or None,
        reasoning=None,
    )
