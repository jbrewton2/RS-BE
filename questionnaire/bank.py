import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.config import QUESTION_BANK_PATH
from questionnaire.models import QuestionBankEntryModel


STORE_KEY = "stores/question_bank.json"
SCHEMA_VERSION = 1


def _unwrap_bank_payload(candidate: Any) -> List[Dict[str, Any]]:
    """
    Accept both:
      A) legacy list format: [ {entry}, {entry}, ... ]
      B) wrapped format: { "schema_version": 1, "item_count": N, "items": [ ... ] }
    Return a list of dict entries.
    """
    if candidate is None:
        return []

    if isinstance(candidate, list):
        # legacy list
        return [x for x in candidate if isinstance(x, dict)]

    if isinstance(candidate, dict):
        items = candidate.get("items")
        if isinstance(items, list):
            return [x for x in items if isinstance(x, dict)]

    return []


def _parse_json_text(raw_text: str) -> List[Dict[str, Any]]:
    raw_text = (raw_text or "").strip()
    if not raw_text:
        return []
    try:
        candidate = json.loads(raw_text)
    except Exception:
        return []
    return _unwrap_bank_payload(candidate)


def load_question_bank(storage: Any) -> List[QuestionBankEntryModel]:
    """
    Load question bank entries.

    Preferred: StorageProvider key "stores/question_bank.json"
    Fallback: legacy filesystem QUESTION_BANK_PATH
    """

    raw_items: List[Dict[str, Any]] = []

    # 1) StorageProvider (preferred)
    try:
        # storage injected by caller
        raw_bytes = storage.get_object(STORE_KEY)
        raw_text = raw_bytes.decode("utf-8", errors="ignore") if raw_bytes else ""
        raw_items = _parse_json_text(raw_text)
    except Exception:
        raw_items = []

    # 2) Legacy filesystem fallback if storage read produced nothing
    if not raw_items:
        try:
            if os.path.exists(QUESTION_BANK_PATH):
                with open(QUESTION_BANK_PATH, "r", encoding="utf-8") as f:
                    candidate = json.load(f)
                raw_items = _unwrap_bank_payload(candidate)
        except Exception:
            raw_items = []

    entries: List[QuestionBankEntryModel] = []
    for item in raw_items:
        try:
            # tolerate older/alt field names
            normalized: Dict[str, Any] = dict(item)

            # Some past code used primaryTag instead of primary_tag
            if "primary_tag" not in normalized and "primaryTag" in normalized:
                normalized["primary_tag"] = normalized.get("primaryTag")

            # frameworks may be null
            fw = normalized.get("frameworks")
            if fw is None:
                normalized["frameworks"] = []
            elif isinstance(fw, str):
                normalized["frameworks"] = [fw]
            elif not isinstance(fw, list):
                normalized["frameworks"] = [str(fw)]
            else:
                normalized["frameworks"] = [str(x) for x in fw if str(x).strip()]

            # variants may be null
            v = normalized.get("variants")
            if v is None:
                normalized["variants"] = []
            elif isinstance(v, str):
                normalized["variants"] = [v]
            elif not isinstance(v, list):
                normalized["variants"] = [str(v)]
            else:
                normalized["variants"] = [str(x) for x in v if str(x).strip()]

            # status default
            if not normalized.get("status"):
                normalized["status"] = "approved"

            # Ensure required fields exist for model validation
            if not normalized.get("id"):
                # If an entry somehow lacks id, skip it (bank ids matter for matching)
                continue
            if "text" not in normalized or "answer" not in normalized:
                continue

            entries.append(QuestionBankEntryModel(**normalized))
        except Exception:
            # skip malformed entries rather than failing the whole bank
            continue

    return entries


def save_question_bank(storage: Any, entries: List[QuestionBankEntryModel]) -> None:
    """
    Persist the bank in WRAPPED canonical format to:
      - StorageProvider key "stores/question_bank.json"
      - Legacy filesystem fallback QUESTION_BANK_PATH (best-effort)
    """
    # Serialize entries
    serializable: List[Dict[str, Any]] = []
    for e in entries or []:
        try:
            serializable.append(e.model_dump())
        except Exception:
            # last resort
            serializable.append(
                {
                    "id": getattr(e, "id", None),
                    "text": getattr(e, "text", None),
                    "answer": getattr(e, "answer", None),
                }
            )

    wrapper: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "item_count": len(serializable),
        "items": serializable,
    }

    payload = json.dumps(wrapper, indent=2, ensure_ascii=False).encode("utf-8", errors="ignore")

    # 1) StorageProvider (preferred)
    try:
        storage.put_object(
            key=STORE_KEY,
            data=payload,
            content_type="application/json",
            metadata=None,
        )
    except Exception:
        pass

    # 2) Legacy filesystem fallback (best-effort)
    try:
        os.makedirs(os.path.dirname(QUESTION_BANK_PATH), exist_ok=True)
        with open(QUESTION_BANK_PATH, "w", encoding="utf-8") as f:
            json.dump(wrapper, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


# ---------------------------------------------------------------------
# Legacy helpers required by other modules (router/service)
# NOTE: keep these here to avoid import breakage during refactors.
# ---------------------------------------------------------------------
import re

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
    s = s.replace("\u00A0", " ")   # NBSP -> space
    s = s.strip()
    s = re.sub(r"\s+", " ", s)
    return s

# Some older code paths refer to normalize_tag; keep it equivalent.
def normalize_tag(s: str) -> str:
    return normalize_text(s)
