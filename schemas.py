# backend/schemas.py
from __future__ import annotations

from typing import List, Optional, Dict
from pydantic import BaseModel, validator

# -----------------------------------------------------
# CONSTANTS & HELPERS
# -----------------------------------------------------

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

RISK_ACTION_LIST = [
    "NEGOTIATE",
    "ASSIGN_TO_PROJECT",
    "ROUTE_TO_SECURITY",
    "ROUTE_TO_LEGAL",
    "ACCEPT_WITH_MONITORING",
    "OTHER",
]

def _normalize_severity(v):
    if not v: return "Medium"
    v = v.lower().strip()
    if v.startswith("crit"): return "Critical"
    if v.startswith("high"): return "High"
    if v.startswith("low"): return "Low"
    return "Medium"

def _normalize_scope(v):
    if v is None: return None
    v = v.lower().strip()
    if v.startswith("enter"): return "enterprise"
    if v.startswith("proj"): return "project"
    return "unknown"

def _normalize_action(v):
    if not v: return None
    v = v.upper()
    if v in RISK_ACTION_LIST: return v
    if "NEGOTIATE" in v: return "NEGOTIATE"
    if "SECURITY" in v: return "ROUTE_TO_SECURITY"
    if "LEGAL" in v: return "ROUTE_TO_LEGAL"
    if "ACCEPT" in v: return "ACCEPT_WITH_MONITORING"
    return "OTHER"

def _normalize_category(v):
    if not v: return None
    v = v.upper()
    if v in RISK_CATEGORY_LIST: return v
    clean = "".join(c for c in v if c.isalpha() or c == "_")
    if clean in RISK_CATEGORY_LIST: return clean
    return "OTHER"


# -----------------------------------------------------
# MODELS: CONTRACT REVIEW
# -----------------------------------------------------

class HitModel(BaseModel):
    label: str
    severity: str
    lines: List[int] = []


class EvidenceModel(BaseModel):
    line: Optional[int] = None
    text: Optional[str] = None

    @validator("text", pre=True, always=True)
    def ensure_text(cls, v):
        return None if v is None else str(v)


class RiskModel(BaseModel):
    id: Optional[str] = None
    label: str
    category: Optional[str] = None
    severity: str
    document_name: Optional[str] = None
    lines: List[int] = []
    scope: Optional[str] = None
    action: Optional[str] = None
    rationale: str = ""
    evidence: List[EvidenceModel] = []
    related_flags: List[str] = []

    resolution: Optional[str] = None
    resolutionNote: Optional[str] = None

    @validator("severity", pre=True, always=True)
    def v_severity(cls, v): return _normalize_severity(v)

    @validator("scope", pre=True, always=True)
    def v_scope(cls, v): return _normalize_scope(v)

    @validator("action", pre=True, always=True)
    def v_action(cls, v): return _normalize_action(v)

    @validator("category", pre=True, always=True)
    def v_cat(cls, v): return _normalize_category(v)


class AnalyzeRequestModel(BaseModel):
    document_name: str
    text: str
    hits: List[HitModel] = []
    prompt_override: Optional[str] = None
    temperature: Optional[float] = 0.2
    model: Optional[str] = None

    # NEW: Knowledge docs for review
    knowledge_doc_ids: Optional[List[str]] = None


class DeliverableModel(BaseModel):
    title: str
    due_date: Optional[str] = None
    frequency: Optional[str] = None
    reference: Optional[str] = None
    notes: Optional[str] = None


class AnalyzeResponseModel(BaseModel):
    summary: str
    risks: List[RiskModel]
    doc_type: Optional[str] = None
    deliverables: List[DeliverableModel] = []


# -----------------------------------------------------
# MODELS: REVIEW OBJECT
# -----------------------------------------------------

class UploadedDocModel(BaseModel):
    id: str
    name: str
    size: Optional[int] = None
    type: Optional[str] = None
    text: Optional[str] = None
    pdfUrl: Optional[str] = None
    pages: Optional[List[Dict[str, int]]] = None


class SavedReviewModel(BaseModel):
    id: str
    name: str
    department: str
    dataType: str
    date: str
    reviewer: str
    docs: List[UploadedDocModel]
    activeDocId: Optional[str] = None

    reviewerNotes: str = ""
    startedAt: Optional[str] = None
    status: Optional[str] = None

    aiSummary: Optional[str] = None
    aiRisks: List[RiskModel] = []
    docSummaries: Dict[str, str] = {}
    lastAnalysisAt: Optional[str] = None


# -----------------------------------------------------
# MODELS: QUESTION BANK
# -----------------------------------------------------

class QuestionBankEntryModel(BaseModel):
    id: str
    short_key: str
    question_text: str
    canonical_answer: str
    tags: List[str] = []
    framework_refs: List[str] = []
    created_at: str
    updated_at: str
    usage_stats: Dict[str, int] = {}
    feedback_notes: List[str] = []


class QuestionBankUpsertRequest(BaseModel):
    id: Optional[str] = None
    short_key: str
    question_text: str
    canonical_answer: str
    tags: List[str] = []
    framework_refs: List[str] = []


# -----------------------------------------------------
# MODELS: QUESTIONNAIRE ANALYSIS
# -----------------------------------------------------

class QuestionnaireQuestionModel(BaseModel):
    id: str
    question_text: str
    matched_bank_id: Optional[str] = None
    suggested_answer: Optional[str] = None
    confidence: float = 0.0
    status: str = "needs_review"
    feedback_status: Optional[str] = None
    feedback_reason: Optional[str] = None


class QuestionnaireAnalysisRequest(BaseModel):
    document_text: str
    source_doc_name: Optional[str] = None


class QuestionnaireAnalysisResponse(BaseModel):
    questions: List[QuestionnaireQuestionModel]
    overall_confidence: float
    total_questions: int


class QuestionnaireFeedbackRequest(BaseModel):
    question_id: str
    matched_bank_id: Optional[str] = None
    approved: bool
    feedback_reason: Optional[str] = None
    final_answer: Optional[str] = None
