from __future__ import annotations

from typing import List, Optional, Literal
from pydantic import BaseModel, Field

# ------------------------------------------
# Type aliases
# ------------------------------------------

AnswerSource = Literal["bank", "llm"]
QuestionStatus = Literal["auto_approved", "needs_review", "low_confidence", "approved"]
FeedbackStatus = Literal["approved", "rejected"]

# Human workflow status (UI driven)
WorkflowStatus = Literal["needs_review", "in_progress", "complete"]

# ------------------------------------------
# Questionnaire Question Model
# ------------------------------------------


class QuestionnaireQuestionModel(BaseModel):
    id: str
    question_text: str

    # Optional tags describing the question / answer domain:
    # e.g. ["CUI", "NIST 800-171", "MFA"]
    #
    # These may come from:
    #   - Manual entry (frontend ManualQuestionBuilder)
    #   - Answer bank (primary_tag + frameworks)
    #   - LLM-inferred tags (questionnaire-batch)
    tags: Optional[List[str]] = None

    # Suggested answer from bank or LLM
    suggested_answer: Optional[str] = None
    confidence: Optional[float] = None
    answer_source: Optional[AnswerSource] = None

    # LLM / bank status
    status: Optional[QuestionStatus] = None

    # Bank linkage
    matched_bank_id: Optional[str] = None

    # Feedback from UI workflow
    feedback_status: Optional[FeedbackStatus] = None
    feedback_reason: Optional[str] = None

    # NEW: human workflow status (UI / reviewer controlled)
    review_status: Optional[WorkflowStatus] = None

    # Knowledge source traceability:
    # each dict is expected to look like:
    # {
    #   "doc_id": "...",
    #   "title": "...",
    #   "doc_type": "...",
    #   "tags": [...],
    #   "snippet": "..."
    # }
    knowledge_sources: Optional[List[dict]] = None


# ------------------------------------------
# Request/Response Models
# ------------------------------------------


class QuestionnaireAnalyzeRequest(BaseModel):
    raw_text: str
    llm_enabled: bool = True
    knowledge_doc_ids: Optional[List[str]] = None


class AnalyzeQuestionnaireResponse(BaseModel):
    raw_text: str
    questions: List[QuestionnaireQuestionModel]
    overall_confidence: Optional[float] = None


# ------------------------------------------
# Question Bank Models
# ------------------------------------------


class QuestionBankEntryModel(BaseModel):
    id: str
    text: str
    answer: str
    primary_tag: Optional[str] = None
    frameworks: Optional[List[str]] = None
    status: Optional[str] = "approved"  # draft | approved | retired

    # Optional paraphrase variants of the question text, used to improve matching
    variants: List[str] = Field(default_factory=list)

    # Feedback learning fields
    rejection_reasons: List[str] = Field(default_factory=list)
    last_feedback: Optional[str] = None
    usage_count: int = 0
    last_used_at: Optional[str] = None


class QuestionBankUpsertModel(BaseModel):
    id: Optional[str] = None
    text: str
    answer: str
    primaryTag: Optional[str] = None
    frameworks: Optional[List[str]] = None
    status: Optional[str] = "approved"
    # Optional list of paraphrased variants for the question text
    variants: Optional[List[str]] = None

# ------------------------------------------
# Feedback
# ------------------------------------------


class QuestionnaireFeedbackRequest(BaseModel):
    question_id: str
    matched_bank_id: Optional[str] = None
    approved: bool
    feedback_reason: Optional[str] = None
    final_answer: Optional[str] = None

    promote_to_bank: bool = False
    question_text: Optional[str] = None
