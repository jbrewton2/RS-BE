# backend/pricing/status.llm_stats/router.py
from __future__ import annotations

from typing import Dict, List

from fastapi import APIRouter

router = APIRouter(
    prefix="/llm-stats",
    tags=["llm-stats"],
)


@router.get("")
async def get_llm_stats(
    start_date: str | None = None,
    end_date: str | None = None,
    model: str | None = None,
):
    """
    Minimal placeholder LLM stats endpoint so the UI can render.

    You can later wire this up to a real llm_usage.json file or database
    (where _record_llm_usage writes events).
    """
    return {
        "events": [],  # type: List[dict]
        "totalsByModel": {},  # type: Dict[str, dict]
        "totalsByApp": {},
        "totalsByUser": {},
        "totalCost": 0.0,
    }
