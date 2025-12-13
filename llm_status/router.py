# backend/llm_status/router.py
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

# ✅ import pricing helper to compute cost from tokens + pricing config
from ..pricing.llm_pricing_store import compute_cost_usd

BASE_DIR = Path(__file__).resolve().parent.parent
LLM_STATS_FILE = BASE_DIR / "llm_stats.json"

router = APIRouter(prefix="/llm-stats", tags=["llm-stats"])


class LlmStatsBucket(BaseModel):
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_cost: float = 0.0


class LlmStatsSummary(BaseModel):
    by_date: Dict[str, LlmStatsBucket]
    by_app: Dict[str, LlmStatsBucket]
    by_model: Dict[str, LlmStatsBucket]
    grand_totals: LlmStatsBucket


def _load_llm_stats() -> List[Dict[str, Any]]:
    """
    Load raw events from llm_stats.json.
    Expected format: a JSON array of dicts.
    If file is missing or invalid, return an empty list.
    """
    if not LLM_STATS_FILE.exists():
        return []

    try:
        with LLM_STATS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []

    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]

    # If someone wrote a dict or something else, just ignore for now
    return []


def _safe_int(event: Dict[str, Any], key: str) -> int:
    value = event.get(key, 0)
    try:
        if value is None:
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def _safe_float(event: Dict[str, Any], key: str) -> float:
    value = event.get(key, 0.0)
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _get_date_str(event: Dict[str, Any]) -> str:
    """
    Try to parse event["timestamp"] to YYYY-MM-DD.
    Fall back to 'unknown' if parsing fails or missing.
    """
    ts = event.get("timestamp")
    if not ts:
        return "unknown"

    # Most likely ISO8601 from datetime.isoformat()
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt.date().isoformat()
    except Exception:
        # Last resort: take up to first 'T'
        s = str(ts)
        if "T" in s:
            return s.split("T", 1)[0]
        return "unknown"


def _update_bucket(bucket: LlmStatsBucket, event: Dict[str, Any]) -> None:
    bucket.calls += 1
    bucket.input_tokens += _safe_int(event, "input_tokens")
    bucket.output_tokens += _safe_int(event, "output_tokens")

    # We now always inject a per-event total_cost based on pricing.
    # But to stay backwards compatible, still allow existing cost fields if present.
    total_cost = _safe_float(event, "total_cost")
    if not total_cost:
        input_cost = _safe_float(event, "input_cost")
        output_cost = _safe_float(event, "output_cost")
        total_cost = input_cost + output_cost

    bucket.total_cost += total_cost


def _aggregate_events(events: List[Dict[str, Any]]) -> LlmStatsSummary:
    by_date: Dict[str, LlmStatsBucket] = {}
    by_app: Dict[str, LlmStatsBucket] = {}
    by_model: Dict[str, LlmStatsBucket] = {}
    grand = LlmStatsBucket()

    for ev in events:
        # --- NEW: compute cost from pricing config based on tokens + model ---
        in_tokens = _safe_int(ev, "input_tokens")
        out_tokens = _safe_int(ev, "output_tokens")
        model_name = str(ev.get("model") or "unknown")

        try:
            cost_from_pricing = compute_cost_usd(
                model=model_name,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
            )
        except Exception:
            cost_from_pricing = 0.0

        # Copy event so we don't mutate original list in-place
        ev_with_cost = dict(ev)
        # Override or set total_cost to the pricing-derived cost
        ev_with_cost["total_cost"] = cost_from_pricing

        date_key = _get_date_str(ev_with_cost)
        app_key = str(ev_with_cost.get("app") or "unknown")
        model_key = str(ev_with_cost.get("model") or "unknown")

        if date_key not in by_date:
            by_date[date_key] = LlmStatsBucket()
        if app_key not in by_app:
            by_app[app_key] = LlmStatsBucket()
        if model_key not in by_model:
            by_model[model_key] = LlmStatsBucket()

        _update_bucket(by_date[date_key], ev_with_cost)
        _update_bucket(by_app[app_key], ev_with_cost)
        _update_bucket(by_model[model_key], ev_with_cost)
        _update_bucket(grand, ev_with_cost)

    return LlmStatsSummary(
        by_date=by_date,
        by_app=by_app,
        by_model=by_model,
        grand_totals=grand,
    )


@router.get("", response_model=LlmStatsSummary)
def get_llm_stats() -> LlmStatsSummary:
    """
    Aggregate LLM usage stats from llm_stats.json.
    Returns rollups by date, app, model, plus grand totals.

    total_cost in each bucket is now computed from llm_pricing.json
    using the current pricing config.
    """
    try:
        events = _load_llm_stats()
        return _aggregate_events(events)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to aggregate LLM stats: {exc}") from exc
