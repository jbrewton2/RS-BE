from __future__ import annotations

import csv
import io
import uuid
import hashlib
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, HTTPException, UploadFile, File, Depends
from pydantic import BaseModel

from core.deps import get_storage

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
    """
    snippet_norm = (snippet or "").strip()
    snippet_hash = hashlib.sha1(snippet_norm.encode("utf-8", errors="ignore")).hexdigest()[:12]
    raw = f"{rule_id}|{doc_id}|{_stable_str(start)}|{_stable_str(end)}|{snippet_hash}"
    return "hit_" + hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _ensure_hit_keys(hits: List[dict]) -> List[dict]:
    """
    Ensure every hit includes a deterministic hit_key.
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
    Deterministically select a hit.
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
        raise HTTPException(status_code=500, detail=f"Failed to load flags: {exc}")


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
        raise HTTPException(status_code=500, detail=f"Failed to save flags: {exc}")


# ---------------------------------------------------------------------
# POST /flags/test
# ---------------------------------------------------------------------
@router.post("/test")
async def test_flags(payload: Dict[str, object]):
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Payload must include non-empty 'text'.")

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
# POST /flags/explain  (Option A)
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


def _normalize_rule_id(rule_id: Optional[str]) -> Optional[str]:
    if not rule_id:
        return None
    rid = str(rule_id)

    # Deterministic legacy id mapping (old autoFlags used cyber_dfars_* ids)
    if rid.startswith("cyber_dfars_"):
        return "clause-dfars-" + rid.replace("cyber_dfars_", "")
    return rid


def _build_explanation(
    *,
    rule_id: Optional[str],
    rule_label: str,
    rule_severity: Optional[str],
    rule_category: Optional[str],
    rule_tip: Optional[str],
    hit_key: Optional[str],
    matched: Optional[str],
    doc_id: Optional[str],
) -> str:
    return (
        "WHY THIS FLAG TRIGGERED\n"
        f"- Flag: {rule_label or (rule_id or 'Unknown rule')}\n"
        f"- Severity: {rule_severity or 'Unknown'}\n"
        f"- Category: {rule_category or 'Unknown'}\n"
        f"- doc_id: {doc_id or 'Unknown'}\n"
        f"- hit_key: {hit_key or 'Unknown'}\n\n"
        "EVIDENCE (MATCHED TEXT)\n"
        f"- {matched or 'Not available'}\n\n"
        "WHY IT MATTERS\n"
        f"- {rule_tip or 'This language may impose operational, compliance, or delivery obligations.'}\n\n"
        "WHAT TO DO NEXT\n"
        "- Confirm applicability with the contract section and surrounding context.\n"
        "- Capture required actions/owners in your risk register.\n"
        "- If unclear, route to the appropriate owner team for disposition.\n"
    )


@router.post("/explain", response_model=FlagExplainResponse)
async def explain_flag_hit(body: FlagExplainRequest, storage=Depends(get_storage)):
    """
    Deterministic explain path.

    Option A behavior:
      - If rule metadata is missing, DO NOT 404.
      - Return a safe, deterministic explanation using hit fields as fallback.
    """
    reviews = _read_reviews_file(storage)
    review = next((r for r in reviews if r.get("id") == body.review_id), None)
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")

    hits = (review.get("autoFlags") or {}).get("hits") or []
    if not hits:
        raise HTTPException(status_code=404, detail="No auto flag hits")

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
    rules = (flags_payload.clause + flags_payload.context)

    # Prefer hit id, then stored flag_id, then request flag_id
    raw_rule_id = hit.get("id") or hit.get("flag_id") or body.flag_id
    rule_id = _normalize_rule_id(raw_rule_id)

    rule = None
    if rule_id:
        rule = next((r for r in rules if r.id == rule_id), None)

    # Fallback: match by label if ids drifted
    if not rule:
        hit_label = (hit.get("label") or "").strip()
        if hit_label:
            rule = next((r for r in rules if (getattr(r, "label", "") or "").strip() == hit_label), None)

    matched = (hit.get("matched_text") or hit.get("snippet") or hit.get("match") or "").strip() or None
    hit_key = hit.get("hit_key")
    doc_id = (hit.get("doc_id") or hit.get("docId") or body.doc_id or "").strip() or None

    # ---- Option A: Never 404 due to missing rule ----
    if not rule:
        fallback_label = (hit.get("label") or "").strip() or (rule_id or "Unknown rule")
        fallback_sev = (hit.get("severity") or "").strip() or None
        fallback_cat = (hit.get("category") or "").strip() or None
        fallback_tip = (
            "Rule metadata was not found in the flags library for this hit. "
            "Treat this as a deterministic finding based on matched text; validate the contract context."
        )

        explanation = _build_explanation(
            rule_id=rule_id,
            rule_label=fallback_label,
            rule_severity=fallback_sev,
            rule_category=fallback_cat,
            rule_tip=fallback_tip,
            hit_key=hit_key,
            matched=matched,
            doc_id=doc_id,
        )

        reasoning = (
            f"rule_not_found_fallback: rule_id={rule_id}, hit_key={hit_key}. "
            f"Returned deterministic explanation using hit fields."
        )

        return FlagExplainResponse(
            explanation=explanation,
            flaggedText=matched,
            reasoning=reasoning,
        )

    # ---- Normal path with rule metadata ----
    explanation = _build_explanation(
        rule_id=rule.id,
        rule_label=rule.label or rule.id,
        rule_severity=getattr(rule, "severity", None),
        rule_category=getattr(rule, "category", None),
        rule_tip=getattr(rule, "tip", None),
        hit_key=hit_key,
        matched=matched,
        doc_id=doc_id,
    )

    reasoning = f"resolved_rule: rule_id={rule.id}, hit_key={hit_key}"
    return FlagExplainResponse(explanation=explanation, flaggedText=matched, reasoning=reasoning)
