# rag/contracts.py
from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

# Modes remain backward-compatible. Keep tight/explicit.
RagMode = Literal["review_summary"]

AnalysisIntent = Literal["strict_summary", "risk_triage"]
ContextProfile = Literal["fast", "balanced", "deep"]


class RagAnalyzeRequest(BaseModel):
    """
    Request body for POST /api/rag/analyze
    """

    review_id: str = Field(..., min_length=1)
    mode: RagMode = Field(default="review_summary")

    # Behavior intent
    analysis_intent: AnalysisIntent = Field(
        default="strict_summary",
        description="strict_summary = conservative, contract-locked sections; risk_triage = broader human-in-loop risk surfacing.",
    )

    # Retrieval breadth (independent of RAG_FAST)
    context_profile: ContextProfile = Field(
        default="fast",
        description="fast|balanced|deep. Deep overrides fast caps to allow scanning for risks.",
    )

    # Retrieval knobs
    top_k: int = Field(default=12, ge=1, le=50)
    force_reingest: bool = Field(default=False)

    # Debug: include compact per-question hit lists
    debug: bool = Field(default=False)


class RagCitation(BaseModel):
    question: str
    doc: str
    docId: str
    charStart: int
    charEnd: int
    score: float
    snippet: Optional[str] = None


class RagEvidenceSnippet(BaseModel):
    docId: str
    doc: Optional[str] = None
    text: str
    charStart: Optional[int] = None
    charEnd: Optional[int] = None
    score: Optional[float] = None


class RagSection(BaseModel):
    id: str
    owner: Optional[str] = None  # Security/ISSO | Legal/Contracts | Program/PM | Engineering | Finance | QA

    title: str

    findings: List[str] = Field(default_factory=list)
    evidence: List[RagEvidenceSnippet] = Field(default_factory=list)
    gaps: List[str] = Field(default_factory=list)
    recommended_actions: List[str] = Field(default_factory=list)

    # Optional UI hint
    confidence: Optional[Literal["strong", "moderate", "weak", "missing"]] = None


class RagAnalyzeStats(BaseModel):
    top_k_effective: Optional[int] = None
    analysis_intent: Optional[str] = None
    context_profile: Optional[str] = None

    retrieved_total: Optional[int] = None
    context_max_chars: Optional[int] = None
    context_used_chars: Optional[int] = None
    context_truncated: Optional[bool] = None

    fast_mode: Optional[bool] = None


class RagAnalyzeResponse(BaseModel):
    review_id: str
    mode: RagMode
    top_k: int

    analysis_intent: AnalysisIntent
    context_profile: ContextProfile

    summary: str

    citations: List[RagCitation] = Field(default_factory=list)
    retrieved_counts: Dict[str, int] = Field(default_factory=dict)

    sections: Optional[List[RagSection]] = None
    stats: Optional[RagAnalyzeStats] = None
    warnings: List[str] = Field(default_factory=list)

    # Debug payload (only when debug=true)
    retrieved: Optional[Dict[str, list]] = None
