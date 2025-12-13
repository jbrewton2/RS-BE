# backend/llm_status/store.py
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

BASE_DIR = Path(__file__).resolve().parent.parent
LLM_STATS_FILE = BASE_DIR / "llm_stats.json"


def _load_events() -> List[Dict[str, Any]]:
    """
    Load the current list of LLM events from llm_stats.json.
    If the file is missing or invalid, return an empty list.
    """
    if not LLM_STATS_FILE.exists():
        return []

    try:
        with LLM_STATS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []

    if isinstance(data, list):
        # Only keep dict items
        return [e for e in data if isinstance(e, dict)]

    # If it was a dict or something else, ignore and reset
    return []


def append_llm_event(event: Dict[str, Any]) -> None:
    """
    Append a single LLM event to llm_stats.json as part of a list.
    Shape is intentionally loose: the aggregator (/llm-stats)
    will only look at a few keys (timestamp, app, model, tokens, cost).
    """
    events = _load_events()

    # Ensure timestamp is present
    if "timestamp" not in event:
        event["timestamp"] = datetime.now(timezone.utc).isoformat()

    events.append(event)

    with LLM_STATS_FILE.open("w", encoding="utf-8") as f:
        json.dump(events, f, indent=2, default=str)
