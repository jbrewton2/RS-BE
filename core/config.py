# backend/core/config.py
from __future__ import annotations

import os

# ----------------------------------------------------
# PDF / DOCX libraries (shared by backend.main + others)
# ----------------------------------------------------

try:
    from pypdf import PdfReader as _PdfReader
except ImportError:  # pypdf not installed
    _PdfReader = None

try:
    import docx as _docx  # python-docx
except ImportError:  # python-docx not installed
    _docx = None

# These are what backend.main imports
PdfReader = _PdfReader
docx = _docx

# ----------------------------------------------------
# Base directories & file paths
# ----------------------------------------------------

# This file is backend/core/config.py → BASE_DIR = backend/
BASE_DIR = os.path.dirname(os.path.dirname(__file__))

# Files dir for uploaded PDFs
FILES_DIR = os.path.join(BASE_DIR, "files")
os.makedirs(FILES_DIR, exist_ok=True)

# Reviews + question bank + knowledge store
REVIEWS_FILE = os.path.join(BASE_DIR, "reviews.json")
QUESTION_BANK_PATH = os.path.join(BASE_DIR, "question_bank.json")
KNOWLEDGE_STORE_FILE = os.path.join(BASE_DIR, "knowledge_store.json")
KNOWLEDGE_DOCS_DIR = os.path.join(BASE_DIR, "knowledge_docs")
os.makedirs(KNOWLEDGE_DOCS_DIR, exist_ok=True)

# ----------------------------------------------------
# LLM config defaults (ENV FIRST, SAFE DEFAULTS)
# ----------------------------------------------------

# IMPORTANT:
# - Prefer env vars at runtime (Docker / deployment)
# - Default to Ollama /api/generate (most compatible)
OLLAMA_API_URL = os.getenv(
    "OLLAMA_API_URL",
    "http://localhost:11434/api/generate",
)

DEFAULT_LLM_MODEL = os.getenv(
    "OLLAMA_MODEL",
    "llama3.1:8b-instruct-q4_K_M",
)

# ----------------------------------------------------
# Risk categories (shared)
# ----------------------------------------------------

RISK_CATEGORY_LIST = [
    "DATA_CLASSIFICATION",
    "CYBER_DFARS",
    "INCIDENT_REPORTING",
    "FLOWDOWN",
    "LIABILITY",
    "SLA",
    "TERMINATION",
    "IP",
    "PRIVACY",
    "OTHER",
]

# ----------------------------------------------------
# System prompts (review vs questionnaire)
# ----------------------------------------------------

SYSTEM_PROMPT_BASE = """
You are an expert contract, cybersecurity, and privacy reviewer for a large
U.S. government contractor.

You are reviewing one or more CONTRACT DOCUMENTS (RFP, RFI, SOW, PWS, draft
contract, attachments, etc.). Your job is to describe WHAT THESE DOCUMENTS
REQUIRE — not to describe a generic service or the contractor's general posture.

Return a concise, plain-text summary in the following sections:

OBJECTIVE
SCOPE
KEY REQUIREMENTS
KEY RISKS
GAPS AND AMBIGUITIES
RECOMMENDED NEXT STEPS
""".strip()

# High-level organization posture that can be referenced by LLMs.
ORG_POSTURE_SUMMARY = """
The organization is a U.S. defense contractor responsible for safeguarding
Controlled Unclassified Information (CUI) in accordance with applicable federal
and DoD requirements.

Key posture points:
- NIST SP 800-171 aligned
- DFARS 252.204-7012 compliant
- Strong access control and authentication
- Encryption at rest and in transit
- Documented SSP, SCRM, and security policies
""".strip()

# Used for questionnaire batch + single question
QUESTIONNAIRE_SYSTEM_PROMPT = """
You are a senior Security and Compliance engineer at a U.S. defense contractor.
Your job is to answer security and SCRM questionnaires conservatively and
accurately using NIST 800-171 and DFARS guidance.

Respond only with the requested output format.
""".strip()

# Used by review analysis (call_llm_for_review)
REVIEW_SYSTEM_PROMPT = """
You are an expert contract analyst specializing in DFARS, NIST 800-171,
SOW/PWS analysis, and risk identification for U.S. Government contractors.

Produce a structured assessment:
OBJECTIVE
SCOPE
KEY REQUIREMENTS
KEY RISKS
GAPS AND AMBIGUITIES
RECOMMENDED NEXT STEPS

Rules:
- Do not hallucinate certifications
- Be conservative and explicit
- Use assessment language, not marketing
""".strip()
