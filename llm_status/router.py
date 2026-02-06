from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

# AUTH
from auth.jwt import get_current_user

# Pricing helper to compute cost from tokens + pricing config
from pricing.llm_pricing_store import compute_cost_usd

BASE_DIR = Path(__file__).resolve().parent.parent
LLM_STATS_FILE = BASE_DIR / "llm_stats.json"

router = APIRouter(
    prefix="/llm-status",              # ✅ CORRECT ROUTE
    tags=["llm-status"],
    dependencies=[Depends(get_current_user)],  # 🔐 protected
)


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
    if not LLM_STATS_FILE.exists():
        return []

    try:
        with LLM_STATS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []

    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]

    return []


def _safe_int(event: Dict[str, Any], key: str) -> int:
    try:
        return int(event.get(key) or 0)
    except Exception:
        return 0


def _safe_float(event: Dict[str, Any], key: str) -> float:
    try:
        return float(event.get(key) or 0.0)
    except Exception:
        return 0.0


def _get_date_str(event: Dict[str, Any]) -> str:
    ts = event.get("timestamp")
    if not ts:
        return "unknown"

    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt.date().isoformat()
    except Exception:
        s = str(ts)
        if "T" in s:
            return s.split("T", 1)[0]
        return "unknown"


def _update_bucket(bucket: LlmStatsBucket, event: Dict[str, Any]) -> None:
    bucket.calls += 1
    bucket.input_tokens += _safe_int(event, "input_tokens")
    bucket.output_tokens += _safe_int(event, "output_tokens")

    total_cost = _safe_float(event, "total_cost")
    if not total_cost:
        total_cost = (
            _safe_float(event, "input_cost")
            + _safe_float(event, "output_cost")
        )

    bucket.total_cost += total_cost


def _aggregate_events(events: List[Dict[str, Any]]) -> LlmStatsSummary:
    by_date: Dict[str, LlmStatsBucket] = {}
    by_app: Dict[str, LlmStatsBucket] = {}
    by_model: Dict[str, LlmStatsBucket] = {}
    grand = LlmStatsBucket()

    for ev in events:
        in_tokens = _safe_int(ev, "input_tokens")
        out_tokens = _safe_int(ev, "output_tokens")
        model_name = str(ev.get("model") or "unknown")

        try:
            cost = compute_cost_usd(
                model=model_name,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
            )
        except Exception:
            cost = 0.0

        ev = dict(ev)
        ev["total_cost"] = cost

        date_key = _get_date_str(ev)
        app_key = str(ev.get("app") or "unknown")
        model_key = model_name

        by_date.setdefault(date_key, LlmStatsBucket())
        by_app.setdefault(app_key, LlmStatsBucket())
        by_model.setdefault(model_key, LlmStatsBucket())

        _update_bucket(by_date[date_key], ev)
        _update_bucket(by_app[app_key], ev)
        _update_bucket(by_model[model_key], ev)
        _update_bucket(grand, ev)

    return LlmStatsSummary(
        by_date=by_date,
        by_app=by_app,
        by_model=by_model,
        grand_totals=grand,
    )


@router.get("", response_model=LlmStatsSummary)
def get_llm_status() -> LlmStatsSummary:
    """
    Aggregate LLM usage + cost stats.
    """
    try:
        events = _load_llm_stats()
        return _aggregate_events(events)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to aggregate LLM status: {exc}",
        )

