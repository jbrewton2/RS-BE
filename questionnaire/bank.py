# backend/questionnaire/bank.py
from __future__ import annotations

import json
import os
import unicodedata
from typing import List, Optional

from backend.core.config import QUESTION_BANK_PATH
from backend.questionnaire.models import QuestionBankEntryModel


def normalize_text(value: Optional[str]) -> str:
    """
    Normalize text so it renders cleanly in the UI and avoids the '�' character.

    - Unicode normalize (NFKC)
    - Replace curly quotes with straight quotes
    - Replace en/em dashes with hyphen
    - Replace non-breaking space with normal space
    - Remove replacement chars and control chars
    - Trim and collapse multiple spaces
    """
    if value is None:
        return ""

    s = str(value)

    # Unicode normalize
    s = unicodedata.normalize("NFKC", s)

    # Curly single quotes (‘ ’ ‚ ‛) -> '
    s = s.replace("\u2018", "'").replace("\u2019", "'") \
         .replace("\u201A", "'").replace("\u201B", "'")

    # Curly double quotes (“ ” „ ‟) -> "
    s = s.replace("\u201C", '"').replace("\u201D", '"') \
         .replace("\u201E", '"').replace("\u201F", '"')

    # En dash / em dash -> hyphen
    s = s.replace("\u2013", "-").replace("\u2014", "-")

    # Non-breaking space -> normal space
    s = s.replace("\u00A0", " ")

    # Remove replacement char and other control chars (except \n and \t)
    s = s.replace("\uFFFD", "")
    s = "".join(
        ch for ch in s
        if ch == "\n"
        or ch == "\t"
        or (0x20 <= ord(ch) != 0x7F)
    )

    # Collapse multiple spaces
    while "  " in s:
        s = s.replace("  ", " ")

    return s.strip()


def _normalize_entry_in_place(entry: QuestionBankEntryModel) -> None:
    """
    Normalize all string fields of a QuestionBankEntryModel in-place.
    """
    entry.text = normalize_text(entry.text)
    entry.answer = normalize_text(entry.answer)
    entry.primary_tag = normalize_text(entry.primary_tag) or None

    # Frameworks / variants / rejection_reasons as cleaned lists
    entry.frameworks = [
        normalize_text(f) for f in (entry.frameworks or [])
        if normalize_text(f)
    ]
    entry.variants = [
        normalize_text(v) for v in (getattr(entry, "variants", []) or [])
        if normalize_text(v)
    ]
    entry.rejection_reasons = [
        normalize_text(r) for r in (getattr(entry, "rejection_reasons", []) or [])
        if normalize_text(r)
    ]

    # last_feedback is optional string
    if getattr(entry, "last_feedback", None) is not None:
        entry.last_feedback = normalize_text(entry.last_feedback) or None

    # status should stay simple but normalize just in case
    if entry.status:
        entry.status = normalize_text(entry.status)


def load_question_bank() -> List[QuestionBankEntryModel]:
    if not os.path.exists(QUESTION_BANK_PATH):
        return []

    try:
        with open(QUESTION_BANK_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return []

    entries: List[QuestionBankEntryModel] = []
    for item in raw:
        try:
            entry = QuestionBankEntryModel(**item)
        except Exception:
            continue

        # Normalize on load (helps clean older data)
        _normalize_entry_in_place(entry)
        entries.append(entry)

    return entries


def save_question_bank(entries: List[QuestionBankEntryModel]) -> None:
    # Normalize before saving (so any updates are also cleaned)
    for e in entries:
        _normalize_entry_in_place(e)

    serializable = [e.model_dump() for e in entries]
    with open(QUESTION_BANK_PATH, "w", encoding="utf-8") as f:
        # keep ensure_ascii=False for proper UTF-8, but our text is already clean
        json.dump(serializable, f, indent=2, ensure_ascii=False)
