# questionnaire/bank.py
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from questionnaire.models import QuestionBankEntryModel

# ---------------------------------------------------------------------
# Storage key (canonical)
# ---------------------------------------------------------------------
QUESTION_BANK_KEY = "stores/question_bank.json"

# ---------------------------------------------------------------------
# Legacy filesystem location (used by older builds / seed / fallback)
#
# In your container you observed: /app/files/stores/question_bank.json
# so we mirror that relative layout from repo root.
# ---------------------------------------------------------------------
THIS_DIR = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(THIS_DIR, ".."))
LEGACY_BANK_PATH = os.path.abspath(os.path.join(REPO_ROOT, "files", "stores", "question_bank.json"))

# Some older code referenced a path from core.config; if it exists, use it.
try:
    from core.config import QUESTION_BANK_PATH as CONFIG_BANK_PATH  # type: ignore
except Exception:
    CONFIG_BANK_PATH = None

# Prefer config path if defined and points somewhere (keeps older envs working)
if isinstance(CONFIG_BANK_PATH, str) and CONFIG_BANK_PATH.strip():
    QUESTION_BANK_PATH = CONFIG_BANK_PATH
else:
    QUESTION_BANK_PATH = LEGACY_BANK_PATH


# ---------------------------------------------------------------------
# Legacy helpers required by other modules (router/service)
# ---------------------------------------------------------------------
def normalize_text(s: str) -> str:
    """
    Normalizes free-text for stable matching and storage.
    - Converts None to ""
    - Strips leading/trailing whitespace
    - Collapses internal whitespace to single spaces
    """
    if s is None:
        return ""
    s = str(s)
    s = s.replace("\u00A0", " ")  # NBSP -> space
    s = s.strip()
    s = re.sub(r"\s+", " ", s)
    return s


def normalize_tag(s: str) -> str:
    return normalize_text(s)


# ---------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------
def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _log(msg: str) -> None:
    # Keep logs lightweight; only prints when something is off
    print(f"[QUESTION_BANK] {msg}", file=sys.stderr)


def _parse_bank_json(raw_text: str) -> Tuple[List[Dict[str, Any]], str]:
    """
    Parses either:
      A) Wrapper dict: {"schema_version":1, "updated_at":"...", "item_count":N, "items":[...]}
      B) Legacy list: [{...}, {...}]
    Returns: (items_list, detected_format)
    """
    raw_text = (raw_text or "").strip()
    if not raw_text:
        return ([], "empty")

    try:
        candidate = json.loads(raw_text)
    except Exception:
        return ([], "invalid-json")

    if isinstance(candidate, dict):
        items = candidate.get("items")
        if isinstance(items, list):
            return (items, "wrapper")
        # Some older shapes used data/items; be tolerant
        data = candidate.get("data")
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return (data["items"], "wrapper-data-items")
        return ([], "dict-unknown")

    if isinstance(candidate, list):
        return (candidate, "legacy-list")

    return ([], "unknown")


def _items_to_models(items: List[Dict[str, Any]]) -> List[QuestionBankEntryModel]:
    out: List[QuestionBankEntryModel] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        try:
            # tolerate older snake/camel variations
            # (your models expect snake_case per OpenAPI)
            normalized = dict(item)

            # common fallbacks
            if "primaryTag" in normalized and "primary_tag" not in normalized:
                normalized["primary_tag"] = normalized.get("primaryTag")
            if "rejectionReasons" in normalized and "rejection_reasons" not in normalized:
                normalized["rejection_reasons"] = normalized.get("rejectionReasons")
            if "lastFeedback" in normalized and "last_feedback" not in normalized:
                normalized["last_feedback"] = normalized.get("lastFeedback")
            if "usageCount" in normalized and "usage_count" not in normalized:
                normalized["usage_count"] = normalized.get("usageCount")
            if "lastUsedAt" in normalized and "last_used_at" not in normalized:
                normalized["last_used_at"] = normalized.get("lastUsedAt")

            # required fields for model: id, text, answer
            if not normalized.get("id") or not normalized.get("text") or not normalized.get("answer"):
                continue

            out.append(QuestionBankEntryModel(**normalized))
        except Exception:
            # skip bad rows, don't crash the app
            continue

    # Dedup by id (keep the last occurrence)
    merged: Dict[str, QuestionBankEntryModel] = {}
    for e in out:
        merged[e.id] = e
    return list(merged.values())


def _models_to_wrapper(entries: List[QuestionBankEntryModel]) -> Dict[str, Any]:
    items = []
    for e in entries:
        # Pydantic v2 model_dump; fallback to dict
        if hasattr(e, "model_dump"):
            items.append(e.model_dump())
        else:
            items.append(asdict(e))  # type: ignore

    return {
        "schema_version": 1,
        "updated_at": _utc_now_iso(),
        "item_count": len(items),
        "items": items,
    }


def _write_local_bank(entries: List[QuestionBankEntryModel]) -> None:
    """
    Write a legacy LIST file locally for backward compatibility.
    This keeps /app/files/stores/question_bank.json refreshed for older tooling.
    """
    try:
        os.makedirs(os.path.dirname(QUESTION_BANK_PATH), exist_ok=True)
        payload = [e.model_dump() if hasattr(e, "model_dump") else asdict(e) for e in entries]  # type: ignore
        with open(QUESTION_BANK_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
    except Exception as e:
        _log(f"local write failed: {e!r}")


# ---------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------
def load_question_bank(storage) -> List[QuestionBankEntryModel]:
    """
    Load question bank.

    Preferred: StorageProvider key "stores/question_bank.json"
      - supports wrapper schema OR legacy list
    Fallback: local filesystem (QUESTION_BANK_PATH) legacy list

    Bridge behavior:
      If storage object exists but contains 0 items AND local file contains items,
      automatically seed storage with local entries (one-time migration).
    """
    storage_items: List[Dict[str, Any]] = []
    storage_fmt = "none"
    storage_raw = None

    # 1) StorageProvider (preferred)
    try:
        raw_bytes = storage.get_object(QUESTION_BANK_KEY)
        storage_raw = (raw_bytes or b"").decode("utf-8", errors="ignore")
        storage_items, storage_fmt = _parse_bank_json(storage_raw)
    except Exception as e:
        storage_items, storage_fmt = ([], "storage-miss")
        # do not spam logs on normal dev cold-starts
        # _log(f"storage read failed ({QUESTION_BANK_KEY}): {e!r}")

    storage_entries = _items_to_models(storage_items)

    # 2) Local filesystem fallback (legacy list)
    local_entries: List[QuestionBankEntryModel] = []
    try:
        if os.path.exists(QUESTION_BANK_PATH):
            with open(QUESTION_BANK_PATH, "r", encoding="utf-8") as f:
                candidate = json.load(f)
            if isinstance(candidate, list):
                local_entries = _items_to_models(candidate)
            elif isinstance(candidate, dict) and isinstance(candidate.get("items"), list):
                local_entries = _items_to_models(candidate["items"])
    except Exception:
        local_entries = []

    # 3) Bridge/migrate if storage is empty but local is not
    if len(storage_entries) == 0 and len(local_entries) > 0:
        _log(
            f"storage bank empty (fmt={storage_fmt}); seeding from local file "
            f"({len(local_entries)} entries) -> {QUESTION_BANK_KEY}"
        )
        try:
            save_question_bank(storage, local_entries)
            return local_entries
        except Exception as e:
            _log(f"seed to storage failed: {e!r}")
            # still return local so app functions
            return local_entries

    # 4) Normal return: prefer storage if it has anything, else local
    if len(storage_entries) > 0:
        return storage_entries
    return local_entries


def save_question_bank(storage, entries: List[QuestionBankEntryModel]) -> None:
    """
    Persist question bank.

    Canonical write:
      - Write WRAPPER schema to MinIO at stores/question_bank.json

    Also write a legacy LIST to local file for backward compatibility.
    """
    # Ensure stable
    entries = entries or []

    wrapper = _models_to_wrapper(entries)
    payload_bytes = json.dumps(wrapper, indent=2, ensure_ascii=False).encode("utf-8", errors="ignore")

    # 1) StorageProvider (preferred)
    storage_ok = False
    try:
        storage.put_object(
            key=QUESTION_BANK_KEY,
            data=payload_bytes,
            content_type="application/json",
            metadata=None,
        )
        storage_ok = True
    except Exception as e:
        _log(f"storage write failed ({QUESTION_BANK_KEY}): {e!r}")

    # 2) Local write (legacy list), regardless of storage result
    _write_local_bank(entries)

    if storage_ok:
        _log(f"saved {len(entries)} entries to storage key={QUESTION_BANK_KEY} (and local legacy file)")
