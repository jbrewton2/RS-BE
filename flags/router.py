from __future__ import annotations

import csv
import io
import uuid
from typing import List, Optional, Dict

from fastapi import APIRouter, HTTPException, UploadFile, File, Depends  # ← AUTH
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

# ### AUTH: JWT dependency
from backend.auth.jwt import get_current_user

# ---------------------------------------------------------------------
# Router (AUTH ENFORCED HERE)
# ---------------------------------------------------------------------

router = APIRouter(
    prefix="/flags",
    tags=["flags"],
    dependencies=[Depends(get_current_user)],  # ← AUTH applied once
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
                rule.patterns = sanitize_patterns(list(rule.patterns))
    return payload


# ---------------------------------------------------------------------
# GET /flags
# ---------------------------------------------------------------------
@router.get("", response_model=FlagsPayload)
async def get_flags():
    """Return the current clause/context flag rules."""
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
    """Replace the entire flags.json payload."""
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
# POST /flags/test
# ---------------------------------------------------------------------
@router.post("/test")
async def test_flags(payload: Dict[str, object]):
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(
            status_code=400,
            detail="Payload must include non-empty 'text'.",
        )

    record_usage = bool(payload.get("record_usage", True))
    return scan_text_for_flags(text, record_usage=record_usage)


# ---------------------------------------------------------------------
# POST /flags/import-csv
# ---------------------------------------------------------------------
@router.post("/import-csv", response_model=FlagsPayload)
async def import_flags_from_csv(file: UploadFile = File(...)):
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

        raw_patterns = [p.strip() for p in (row.get("patterns") or "").split("|") if p.strip()]
        patterns = sanitize_patterns(raw_patterns)

        tip = (row.get("tip") or "").strip()
        severity = (row.get("severity") or "Medium").strip().title()
        category = row.get("category") or None
        scope_hint = row.get("scopeHint") or None

        if severity not in ("Critical", "High", "Medium", "Low"):
            severity = "Medium"

        group = "clause" if group_raw != "context" else "context"

        rule = FlagRule(
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

        (flags_payload.clause if group == "clause" else flags_payload.context).append(rule)

    flags_payload = _sanitize_flags_payload(flags_payload)
    save_flags(flags_payload)
    return flags_payload


# ---------------------------------------------------------------------
# GET /flags/usage
# ---------------------------------------------------------------------
@router.get("/usage")
async def get_flags_usage():
    return get_usage_map()


# ---------------------------------------------------------------------
# POST /flags/explain
# ---------------------------------------------------------------------

class FlagExplainRequest(BaseModel):
    review_id: str
    hit_key: Optional[str] = None
    hit_index: Optional[int] = None
    doc_id: Optional[str] = None
    flag_id: Optional[str] = None
    snippet: Optional[str] = None


class FlagExplainResponse(BaseModel):
    explanation: str
    flaggedText: Optional[str] = None
    reasoning: Optional[str] = None


@router.post("/explain", response_model=FlagExplainResponse)
async def explain_flag_hit(body: FlagExplainRequest):
    reviews = _read_reviews_file()
    review = next((r for r in reviews if r.get("id") == body.review_id), None)
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")

    hits = (review.get("autoFlags") or {}).get("hits") or []
    if not hits:
        raise HTTPException(status_code=404, detail="No auto flag hits")

    hit = hits[0]  # deterministic fallback
    flags_payload = load_flags()
    rule = next(
        (r for r in (flags_payload.clause + flags_payload.context) if r.id == hit.get("id")),
        None,
    )
    if not rule:
        raise HTTPException(status_code=404, detail="Flag rule not found")

    explanation = f'The "{rule.label}" flag was triggered based on matching contract language.'
    return FlagExplainResponse(explanation=explanation)
