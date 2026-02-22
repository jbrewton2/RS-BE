# rag/contracts.py
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

# =============================================================================
# Enums / Literals
# =============================================================================
# Keep tight and backward-compatible.
RagMode = Literal["review_summary"]
AnalysisIntent = Literal["strict_summary", "risk_triage"]
ContextProfile = Literal["fast", "balanced", "deep"]


# =============================================================================
# Request
# =============================================================================
class RagAnalyzeRequest(BaseModel):
    """
    Request body for POST /api/rag/analyze
    """

    review_id: str = Field(..., min_length=1)
    mode: RagMode = Field(default="review_summary")

    analysis_intent: AnalysisIntent = Field(
        default="strict_summary",
        description=(
            "strict_summary = conservative, contract-locked sections; "
            "risk_triage = broader human-in-loop risk surfacing."
        ),
    )

    context_profile: ContextProfile = Field(
        default="fast",
        description="fast|balanced|deep. Deep overrides fast caps to allow scanning for risks.",
    )

    top_k: int = Field(default=12, ge=1, le=50)
    force_reingest: bool = Field(default=False)

    # Optional deterministic Tier-2 signals provided by caller (NOT contract evidence)
    heuristic_hits: Optional[List[Dict[str, Any]]] = Field(default=None)

    # Debug: include extra stats and retrieval debug payload
    debug: bool = Field(default=False)


# =============================================================================
# Evidence / Section Models
# =============================================================================
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

    # Optional UI hints
    confidence: Optional[Literal["strong", "moderate", "weak", "missing"]] = None
    confidence_pct: Optional[int] = None  # 0-100 deterministic confidence


# =============================================================================
# Legacy / Typed Stats (kept for compatibility, but not enforced on response)
# =============================================================================
class RagAnalyzeStats(BaseModel):
    """
    Legacy typed stats. Kept so older code that imports RagAnalyzeStats doesn't break.
    NOTE: The API response now returns stats as Dict[str, Any] to preserve debug keys.
    """

    top_k_effective: Optional[int] = None
    analysis_intent: Optional[str] = None
    context_profile: Optional[str] = None

    retrieved_total: Optional[int] = None
    context_max_chars: Optional[int] = None
    context_used_chars: Optional[int] = None
    context_truncated: Optional[bool] = None

    fast_mode: Optional[bool] = None

    # Materialization stats (AI RiskObjects) – additive
    risk_objects: Optional[Dict[str, int]] = None


# =============================================================================
# Response
# =============================================================================
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
    risks: Optional[List[Dict[str, Any]]] = None

    # IMPORTANT:
    # stats is intentionally untyped to preserve debug payload keys (debug_context, retrieval_debug, ingest, etc.).
    stats: Dict[str, Any] = Field(default_factory=dict)

    warnings: List[str] = Field(default_factory=list)

    # Debug payload (only when debug=true)
    retrieved: Optional[Dict[str, list]] = None