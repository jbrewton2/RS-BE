# flags/store.py
from __future__ import annotations

import json
import os
import uuid
from typing import Any, List, Literal, Optional

from pydantic import BaseModel

FLAGS_FILE = os.path.join(os.path.dirname(__file__), "flags.json")

# Canonical object-store key (MinIO / S3-compatible)
FLAGS_KEY = "stores/flags.json"


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


def _parse_flags_json(raw_text: str) -> FlagsPayload:
    """
    Parse JSON into FlagsPayload (tolerant).
    """
    raw_text = (raw_text or "").strip()
    if not raw_text:
        return FlagsPayload(clause=[], context=[])

    try:
        data = json.loads(raw_text)
    except Exception:
        return FlagsPayload(clause=[], context=[])

    if not isinstance(data, dict):
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


def _load_local_flags() -> FlagsPayload:
    if not os.path.exists(FLAGS_FILE):
        return FlagsPayload(clause=[], context=[])

    try:
        with open(FLAGS_FILE, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except Exception:
        return FlagsPayload(clause=[], context=[])

    return _parse_flags_json(raw)


def _save_local_flags(payload: FlagsPayload) -> None:
    data = {
        "clause": [f.model_dump() for f in (payload.clause or [])],
        "context": [f.model_dump() for f in (payload.context or [])],
    }
    with open(FLAGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_flags(storage: Optional[Any] = None) -> FlagsPayload:
    """
    Load flags.

    Preferred (if storage provided): StorageProvider key "stores/flags.json"
    Fallback: local flags.json packaged with the repo
    """
    # 1) StorageProvider (preferred when passed)
    if storage is not None:
        try:
            raw_bytes = storage.get_object(FLAGS_KEY)
            raw_text = (raw_bytes or b"").decode("utf-8", errors="ignore")
            parsed = _parse_flags_json(raw_text)
            # If storage exists but is empty-ish, fall back to local
            if (parsed.clause or parsed.context):
                return parsed
        except Exception:
            pass

    # 2) Local fallback
    return _load_local_flags()


def save_flags(payload: FlagsPayload, storage: Optional[Any] = None) -> None:
    """
    Persist flags.

    Canonical write (if storage provided): write to StorageProvider at stores/flags.json
    Also best-effort write to local flags.json for backward compatibility.
    """
    payload = payload or FlagsPayload(clause=[], context=[])

    data = {
        "clause": [f.model_dump() for f in (payload.clause or [])],
        "context": [f.model_dump() for f in (payload.context or [])],
    }
    raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8", errors="ignore")

    # 1) StorageProvider
    if storage is not None:
        storage.put_object(
            key=FLAGS_KEY,
            data=raw,
            content_type="application/json",
            metadata=None,
        )

    # 2) Local best-effort
    try:
        _save_local_flags(payload)
    except Exception:
        pass


def new_flag_id() -> str:
    return str(uuid.uuid4())
