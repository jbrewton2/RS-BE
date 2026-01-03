from __future__ import annotations

import json
import os
from typing import Dict

FLAGS_USAGE_FILE = os.path.join(os.path.dirname(__file__), "flags_usage.json")


def _read_usage() -> Dict[str, int]:
    if not os.path.exists(FLAGS_USAGE_FILE):
        return {}
    try:
        with open(FLAGS_USAGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            # ensure all values are ints
            return {k: int(v) for k, v in data.items()}
        return {}
    except Exception:
        return {}


def _write_usage(usage: Dict[str, int]) -> None:
    with open(FLAGS_USAGE_FILE, "w", encoding="utf-8") as f:
        json.dump(usage, f, indent=2, ensure_ascii=False)


def increment_usage_for_flags(flag_ids: list[str]) -> None:
    """
    Increase usage counts for each flag id by 1 for this 'test' event.

    You can adjust semantics later (e.g. add counts by hit, review, etc.).
    """
    usage = _read_usage()
    for fid in flag_ids:
        if not fid:
            continue
        usage[fid] = usage.get(fid, 0) + 1
    _write_usage(usage)


def get_usage_map() -> Dict[str, int]:
    """
    Return a map { flag_id: usage_count }.
    """
    return _read_usage()
