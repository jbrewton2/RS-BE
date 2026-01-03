from __future__ import annotations

from pathlib import Path
import json
from typing import Any, Optional

# -------------------------------------------------
# Optional libs (match historical contract)
# -------------------------------------------------
try:
    # preferred
    from pypdf import PdfReader as _PdfReader
except Exception:
    _PdfReader = None

try:
    # fallback (deprecated but sometimes present)
    from PyPDF2 import PdfReader as _PdfReader2  # type: ignore
except Exception:
    _PdfReader2 = None

try:
    import docx as _docx  # python-docx module
except Exception:
    _docx = None

# Public names expected by main.py and others
PdfReader = _PdfReader or _PdfReader2
docx = _docx

# -------------------------------------------------
# Base Paths
# -------------------------------------------------
BASE_DIR = Path(__file__).resolve().parents[1]

FILES_DIR = BASE_DIR / "files"
FILES_DIR.mkdir(exist_ok=True)

KNOWLEDGE_DOCS_DIR = BASE_DIR / "knowledge_docs"
KNOWLEDGE_DOCS_DIR.mkdir(exist_ok=True)

QUESTION_BANK_PATH = BASE_DIR / "question_bank.json"
KNOWLEDGE_STORE_FILE = BASE_DIR / "knowledge_store.json"
REVIEWS_FILE = BASE_DIR / "reviews.json"

# -------------------------------------------------
# Organization / Posture Defaults
# -------------------------------------------------
ORG_POSTURE_SUMMARY = (
    "Default organizational security posture. "
    "Override via configuration if needed."
)

# -------------------------------------------------
# Safe File Helpers
# -------------------------------------------------
def load_json_file_safe(path: Path, default: Any):
    try:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json_file_safe(path: Path, data: Any) -> None:
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def load_text_file_safe(path: Path) -> str:
    try:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


# -------------------------------------------------
# Public Contract
# -------------------------------------------------
__all__ = [
    # Paths
    "BASE_DIR",
    "FILES_DIR",
    "KNOWLEDGE_DOCS_DIR",
    "QUESTION_BANK_PATH",
    "KNOWLEDGE_STORE_FILE",
    "REVIEWS_FILE",

    # Libraries (historical API)
    "PdfReader",
    "docx",

    # Org config
    "ORG_POSTURE_SUMMARY",

    # Helpers
    "load_json_file_safe",
    "save_json_file_safe",
    "load_text_file_safe",
]
