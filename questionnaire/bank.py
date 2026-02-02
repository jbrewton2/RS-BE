from __future__ import annotations

import json
import os
import re
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

from core.config import QUESTION_BANK_PATH
from questionnaire.models import QuestionBankEntryModel

# Canonical storage key (MinIO / S3 via StorageProvider)
QUESTION_BANK_KEY = "stores/question_bank.json"
SCHEMA_VERSION = 1


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
# Internal: parse the store file (supports legacy list OR wrapped schema)
# ---------------------------------------------------------------------
def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _coerce_items(candidate: Any) -> List[Dict[str, Any]]:
    """
    Returns a list of item dicts from either:
      - legacy list: [ {...}, {...} ]
      - wrapper: { ..., "items": [ {...}, ... ] }
    """
    if candidate is None:
        return []

    # legacy list
    if isinstance(candidate, list):
        return [x for x in candidate if isinstance(x, dict)]

    # wrapper
    if isinstance(candidate, dict):
        items = candidate.get("items")
        if isinstance(items, list):
            return [x for x in items if isinstance(x, dict)]
        # some older wrappers may have "data" -> list
        data = candidate.get("data")
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]

    return []


def _decode_json_text(raw_text: str) -> Any:
    if not raw_text or not raw_text.strip():
        return []
    # handle accidental UTF-8 BOM
    raw_text = raw_text.lstrip("\ufeff")
    return json.loads(raw_text)


def _read_from_storage(storage: Any) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """
    Returns (items, source_note)
    """
    try:
        raw_bytes = storage.get_object(QUESTION_BANK_KEY)
        raw_text = raw_bytes.decode("utf-8", errors="ignore") if raw_bytes else ""
        candidate = _decode_json_text(raw_text)
        return (_coerce_items(candidate), f"storage:{QUESTION_BANK_KEY}")
    except Exception:
        return ([], None)


def _read_from_filesystem() -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """
    Legacy filesystem fallback.
    Expected to be a JSON list OR wrapped schema (we support both).
    """
    if not os.path.exists(QUESTION_BANK_PATH):
        return ([], None)

    try:
        with open(QUESTION_BANK_PATH, "r", encoding="utf-8") as f:
            raw_text = f.read()
        candidate = _decode_json_text(raw_text)
        return (_coerce_items(candidate), f"file:{QUESTION_BANK_PATH}")
    except Exception:
        return ([], None)


def load_question_bank(storage: Any) -> List[QuestionBankEntryModel]:
    """
    Load Question Bank entries.

    Preferred: StorageProvider key "stores/question_bank.json"
    Fallback: legacy filesystem QUESTION_BANK_PATH

    Supports two on-disk formats:
      1) legacy list: [ {...}, {...} ]
      2) canonical wrapper:
         {
           "schema_version": 1,
           "updated_at": "...Z",
           "item_count": N,
           "items": [ {...}, {...} ]
         }
    """
    items: List[Dict[str, Any]] = []
    source = None

    # 1) StorageProvider (preferred)
    if storage is not None:
        items, source = _read_from_storage(storage)

    # 2) Legacy filesystem fallback
    if not items:
        items, source2 = _read_from_filesystem()
        source = source or source2

    entries: List[QuestionBankEntryModel] = []
    for item in items:
        try:
            entries.append(QuestionBankEntryModel(**item))
        except Exception:
            # Skip bad entries rather than failing the whole load
            continue

    # (Optional) deterministic sort for stability
    entries.sort(key=lambda e: (e.id or "", normalize_text(getattr(e, "text", ""))[:120]))
    return entries


def _wrap_entries(entries: List[QuestionBankEntryModel]) -> Dict[str, Any]:
    serializable_items: List[Dict[str, Any]] = []
    for e in entries:
        # Pydantic v2 models support model_dump; v1 uses dict()
        if hasattr(e, "model_dump"):
            serializable_items.append(e.model_dump())
        elif hasattr(e, "dict"):
            serializable_items.append(e.dict())
        else:
            serializable_items.append(asdict(e))  # unlikely, but safe

    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": _now_utc_iso(),
        "item_count": len(serializable_items),
        "items": serializable_items,
    }


def save_question_bank(storage: Any, entries: List[QuestionBankEntryModel]) -> None:
    """
    Persist Question Bank entries.

    Canonical write:
      - StorageProvider: writes WRAPPED schema to stores/question_bank.json

    Compatibility write:
      - Filesystem: writes legacy list to QUESTION_BANK_PATH
        (helps older code paths / dev debugging)
    """
    wrapped = _wrap_entries(entries)

    wrapped_bytes = json.dumps(wrapped, indent=2, ensure_ascii=False).encode("utf-8", errors="ignore")
    legacy_list_bytes = json.dumps(wrapped["items"], indent=2, ensure_ascii=False).encode("utf-8", errors="ignore")

    storage_ok = False

    # 1) StorageProvider write (preferred)
    if storage is not None:
        try:
            storage.put_object(
                key=QUESTION_BANK_KEY,
                data=wrapped_bytes,
                content_type="application/json",
                metadata=None,
            )
            storage_ok = True
        except Exception:
            storage_ok = False

    # 2) Filesystem fallback (always try; helpful even when storage works)
    try:
        os.makedirs(os.path.dirname(QUESTION_BANK_PATH), exist_ok=True)
        with open(QUESTION_BANK_PATH, "wb") as f:
            f.write(legacy_list_bytes)
    except Exception:
        pass

    # If storage was expected and failed, don't silently lie.
    # We don't throw (to avoid breaking runtime), but we at least surface a hint.
    if storage is not None and not storage_ok:
        # Keeping this as print to avoid introducing new logging dependencies.
        print(f"[QUESTION_BANK] WARNING: failed to write to storage key={QUESTION_BANK_KEY}; wrote filesystem fallback only.")


# Convenience: some code paths expect a simple export
def export_question_bank(storage: Any) -> Dict[str, Any]:
    """
    Returns canonical wrapped schema (same shape we persist).
    """
    entries = load_question_bank(storage)
    return _wrap_entries(entries)
