from __future__ import annotations
from core.deps import get_storage

import csv
import io
import uuid
import hashlib
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, HTTPException, UploadFile, File, Depends
from pydantic import BaseModel

from flags.store import (
    FlagRule,
    FlagsPayload,
    load_flags,
    save_flags,
)
from flags.usage_store import get_usage_map
from flags.service import scan_text_for_flags, sanitize_patterns
from reviews.router import _read_reviews_file

# AUTH: JWT dependency
from auth.jwt import get_current_user

# ---------------------------------------------------------------------
# Router (AUTH ENFORCED HERE)
# ---------------------------------------------------------------------

router = APIRouter(
    prefix="/flags",
    tags=["flags"],
    dependencies=[Depends(get_current_user)],
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
# Deterministic hit_key generation
# ---------------------------------------------------------------------
def _stable_str(x: Any) -> str:
    if x is None:
        return ""
    return str(x)


def _make_hit_key(*, rule_id: str, doc_id: str, start: Any, end: Any, snippet: str) -> str:
    """
    Deterministic, stable ID for a specific hit.

    Inputs chosen to remain stable across reloads:
      - rule_id (flag rule id)
      - doc_id
      - match bounds (start/end if present)
      - snippet hash (anchors content)

    Output:
      hit_<sha1prefix>
    """
    snippet_norm = (snippet or "").strip()
    snippet_hash = hashlib.sha1(snippet_norm.encode("utf-8", errors="ignore")).hexdigest()[:12]
    raw = f"{rule_id}|{doc_id}|{_stable_str(start)}|{_stable_str(end)}|{snippet_hash}"
    return "hit_" + hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _ensure_hit_keys(hits: List[dict]) -> List[dict]:
    """
    Ensure every hit includes a deterministic hit_key.

    This is safe to call on old stored data because it will only add hit_key
    if missing.
    """
    out: List[dict] = []
    for h in hits:
        if not isinstance(h, dict):
            continue
        rule_id = _stable_str(h.get("id"))
        doc_id = _stable_str(h.get("doc_id") or h.get("docId") or "")
        snippet = _stable_str(h.get("snippet") or h.get("text") or "")
        start = h.get("start") or h.get("match_start") or h.get("start_idx")
        end = h.get("end") or h.get("match_end") or h.get("end_idx")

        if not h.get("hit_key") and rule_id and doc_id:
            h = dict(h)
            h["hit_key"] = _make_hit_key(
                rule_id=rule_id,
                doc_id=doc_id,
                start=start,
                end=end,
                snippet=snippet,
            )
        out.append(h)
    return out


def _find_hit(
    hits: List[dict],
    *,
    hit_key: Optional[str],
    doc_id: Optional[str],
    flag_id: Optional[str],
    snippet: Optional[str],
    hit_index: Optional[int],
) -> dict:
    """
    Deterministically select a hit, in priority order:

      1) hit_key exact match
      2) (doc_id + flag_id + snippet) match (best-effort stable)
      3) hit_index if valid
      4) fallback first hit

    This prevents "always hits[0]" behavior.
    """
    if not hits:
        raise HTTPException(status_code=404, detail="No auto flag hits")

    # 1) hit_key
    if hit_key:
        for h in hits:
            if isinstance(h, dict) and h.get("hit_key") == hit_key:
                return h

    # 2) doc_id + flag_id + snippet
    if doc_id and flag_id:
        for h in hits:
            if not isinstance(h, dict):
                continue
            hid = _stable_str(h.get("id"))
            hdoc = _stable_str(h.get("doc_id") or h.get("docId") or "")
            if hid == flag_id and hdoc == doc_id:
                if snippet:
                    hs = _stable_str(h.get("snippet") or h.get("text") or "")
                    if snippet.strip() in hs or hs.strip() in snippet.strip():
                        return h
                else:
                    return h

    # 3) hit_index
    if hit_index is not None:
        try:
            idx = int(hit_index)
            if 0 <= idx < len(hits):
                h = hits[idx]
                if isinstance(h, dict):
                    return h
        except Exception:
            pass

    # 4) fallback
    first = hits[0]
    if isinstance(first, dict):
        return first

    raise HTTPException(status_code=404, detail="Unable to select hit")


# ---------------------------------------------------------------------
# GET /flags
# ---------------------------------------------------------------------
@router.get("", response_model=FlagsPayload)
async def get_flags(storage=Depends(get_storage)):
    """Return the current clause/context flag rules."""
    try:
        return load_flags(storage)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to load flags: {exc}",
        )


# ---------------------------------------------------------------------
# PUT /flags
# ---------------------------------------------------------------------
@router.put("", response_model=FlagsPayload)
async def update_flags(payload: FlagsPayload, storage=Depends(get_storage)):
    """Replace the entire flags.json payload."""
    try:
        cleaned = _sanitize_flags_payload(payload)
        save_flags(cleaned, storage)
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
async def import_flags_from_csv(file: UploadFile = File(...), storage=Depends(get_storage)):
    try:
        data = await file.read()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to read CSV: {exc}")

    if not data:
        raise HTTPException(status_code=400, detail="Empty CSV file.")

    text = data.decode("utf-8", errors="ignore")
    reader = csv.DictReader(io.StringIO(text))
    flags_payload = load_flags(storage)

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
    save_flags(flags_payload, storage)
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
async def explain_flag_hit(body: FlagExplainRequest, storage=Depends(get_storage)):
    """
    Deterministic explain path.

    Priority:
      1) hit_key exact match
      2) (doc_id + flag_id + snippet) best-effort match
      3) hit_index
      4) fallback first hit

    NOTE: This is Phase 1 stabilization. Phase 2 will require hit_key (hit_id) always.
    """
    reviews = _read_reviews_file(storage)
    review = next((r for r in reviews if r.get("id") == body.review_id), None)
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")

    hits = (review.get("autoFlags") or {}).get("hits") or []
    if not hits:
        raise HTTPException(status_code=404, detail="No auto flag hits")

    # Ensure hit_key exists for older stored hits
    hits = _ensure_hit_keys(hits)

    hit = _find_hit(
        hits,
        hit_key=body.hit_key,
        doc_id=body.doc_id,
        flag_id=body.flag_id,
        snippet=body.snippet,
        hit_index=body.hit_index,
    )

    flags_payload = load_flags(storage)
    rule = next(
        (r for r in (flags_payload.clause + flags_payload.context) if r.id == hit.get("id")),
        None,
    )
    if not rule:
        raise HTTPException(status_code=404, detail="Flag rule not found")

    matched = (hit.get("matched_text") or hit.get("snippet") or "").strip() or None
    hit_key = hit.get("hit_key")

    # Deterministic explanation template (no LLM here yet)
    explanation = (
        "WHY THIS FLAG TRIGGERED\n"
        f"- Flag: {rule.label}\n"
        f"- Severity: {getattr(rule, 'severity', None)}\n"
        f"- Category: {getattr(rule, 'category', None)}\n"
        f"- hit_key: {hit_key}\n\n"
        "EVIDENCE (MATCHED TEXT)\n"
        f"- {matched or 'Not available'}\n\n"
        "WHY IT MATTERS\n"
        f"- {rule.tip or 'This clause may impose security/compliance obligations.'}\n\n"
        "WHAT TO DO NEXT\n"
        "- Confirm applicability with the contract section and surrounding context.\n"
        "- Capture any required actions/controls in your compliance plan.\n"
    )

    reasoning = (
        f"Selected hit via hit_key/doc_id/flag_id matching. "
        f"rule_id={rule.id}, hit_key={hit_key}"
    )

    return FlagExplainResponse(
        explanation=explanation,
        flaggedText=matched,
        reasoning=reasoning,
    )
















