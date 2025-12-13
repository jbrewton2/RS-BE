# backend/flags_store.py
from __future__ import annotations

import json
import os
import uuid
from typing import List, Literal, Optional

from pydantic import BaseModel

FLAGS_FILE = os.path.join(os.path.dirname(__file__), "flags.json")


class FlagRule(BaseModel):
    """
    Single flag rule used by clause/context flagging.

    - group: "clause" or "context"
    - label: short human readable name
    - patterns: list of regex or plain string patterns to match
    - tip: reviewer guidance rendered in the UI

    Optional metadata:
    - severity: Low | Medium | High | Critical
    - enabled: whether rule is active
    - category: arbitrary string, used for grouping (e.g. CYBER_DFARS, PRIVACY)
    - scopeHint: enterprise|project|unknown scope
    - examples: list of example phrases/matches
    """
    id: str
    group: Literal["clause", "context"]
    label: str
    patterns: List[str]
    tip: str = ""

    severity: Optional[Literal["Low", "Medium", "High", "Critical"]] = "Medium"
    enabled: bool = True
    category: Optional[str] = None
    scopeHint: Optional[Literal["enterprise", "project", "unknown"]] = None
    examples: Optional[List[str]] = None


class FlagsPayload(BaseModel):
    """
    Bucket of all flags: clause + context.
    """
    clause: List[FlagRule] = []
    context: List[FlagRule] = []


def _coerce_flag_dict(raw: dict, group_fallback: Literal["clause", "context"]) -> dict:
    """
    Make raw JSON safe for FlagRule(**...):
    - Ensure group is set.
    - Ensure patterns is always a list of strings.
    - Ensure label/tip/id defaults are provided.
    """
    data = dict(raw) if isinstance(raw, dict) else {}

    if "group" not in data:
        data["group"] = group_fallback

    # patterns: allow string or list
    patterns = data.get("patterns")
    if patterns is None:
        data["patterns"] = []
    elif isinstance(patterns, str):
        data["patterns"] = [patterns]
    elif isinstance(patterns, list):
        data["patterns"] = [str(p) for p in patterns if str(p).strip()]
    else:
        data["patterns"] = [str(patterns)]

    # label / tip fallback
    data["label"] = str(data.get("label") or "Unnamed flag")
    data["tip"] = str(data.get("tip") or "")

    # id: if missing, generate one
    if not data.get("id"):
        data["id"] = str(uuid.uuid4())

    # severity default
    sev = data.get("severity") or "Medium"
    sev_norm = str(sev).capitalize()
    if sev_norm not in ("Low", "Medium", "High", "Critical"):
        sev_norm = "Medium"
    data["severity"] = sev_norm

    return data


def load_flags() -> FlagsPayload:
    """
    Load flags from flags.json.

    If file missing/empty/invalid -> return empty buckets.
    The UI can seed defaults or you can ship a pre-populated flags.json.
    """
    if not os.path.exists(FLAGS_FILE):
        return FlagsPayload(clause=[], context=[])

    try:
        with open(FLAGS_FILE, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except Exception:
        return FlagsPayload(clause=[], context=[])

    if not raw:
        return FlagsPayload(clause=[], context=[])

    try:
        data = json.loads(raw)
    except Exception:
        return FlagsPayload(clause=[], context=[])

    clause_rules: List[FlagRule] = []
    context_rules: List[FlagRule] = []

    for fr in data.get("clause", []) or []:
        try:
            coerced = _coerce_flag_dict(fr, "clause")
            clause_rules.append(FlagRule(**coerced))
        except Exception:
            continue

    for fr in data.get("context", []) or []:
        try:
            coerced = _coerce_flag_dict(fr, "context")
            context_rules.append(FlagRule(**coerced))
        except Exception:
            continue

    return FlagsPayload(clause=clause_rules, context=context_rules)


def save_flags(payload: FlagsPayload) -> None:
    """
    Persist flags.json with the given payload.
    """
    data = {
        "clause": [f.model_dump() for f in (payload.clause or [])],
        "context": [f.model_dump() for f in (payload.context or [])],
    }
    with open(FLAGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def new_flag_id() -> str:
    return str(uuid.uuid4())
