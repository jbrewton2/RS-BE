# rag/contracts.py
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# =============================================================================
# Enums / Literals (keep tight + backward-compatible)
# =============================================================================
RagMode = Literal["review_summary", "default"]
AnalysisIntent = Literal["strict_summary", "risk_triage"]
ContextProfile = Literal["fast", "balanced", "deep"]


# =============================================================================
# Request
# =============================================================================
class RagAnalyzeRequest(BaseModel):
    """
    Request body for POST /api/rag/analyze
    """

    model_config = ConfigDict(extra="allow")

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
    model_config = ConfigDict(extra="allow")

    question: str
    doc: str
    docId: str
    charStart: int
    charEnd: int
    score: float
    snippet: Optional[str] = None


class RagEvidenceSnippet(BaseModel):
    model_config = ConfigDict(extra="allow")

    docId: str
    doc: Optional[str] = None
    text: str
    charStart: Optional[int] = None
    charEnd: Optional[int] = None
    score: Optional[float] = None


class RagSection(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    title: str
    owner: Optional[str] = None  # Security/ISSO | Legal/Contracts | Program/PM | Finance (etc.)

    findings: List[str] = Field(default_factory=list)
    evidence: List[RagEvidenceSnippet] = Field(default_factory=list)
    gaps: List[str] = Field(default_factory=list)
    recommended_actions: List[str] = Field(default_factory=list)

    # Optional UI hints
    confidence: Optional[Literal["strong", "moderate", "weak", "missing"]] = None
    confidence_pct: Optional[int] = None  # 0-100 deterministic confidence


# =============================================================================
# Executive Risk Summary (new)
# =============================================================================
class RagRiskSummary(BaseModel):
    """
    Deterministic executive-level rollup computed server-side.
    Kept typed, but response model is extra-allow so we don't break if keys evolve.
    """

    model_config = ConfigDict(extra="allow")

    overall_level: Optional[str] = None  # Low | Moderate | High
    max_severity: Optional[str] = None   # Informational | Low | Medium | High | Critical
    tier_counts: Dict[str, int] = Field(default_factory=dict)
    by_category: Dict[str, int] = Field(default_factory=dict)
    ai_only_count: Optional[int] = None
    drivers: List[str] = Field(default_factory=list)
    overall_statement: Optional[str] = None


# =============================================================================
# Legacy / Typed Stats (kept so older imports don't break)
# =============================================================================
class RagAnalyzeStats(BaseModel):
    """
    Legacy typed stats. Kept so older code that imports RagAnalyzeStats doesn't break.

    NOTE: The API response intentionally returns stats as Dict[str, Any] to preserve
    debug keys (debug_context, retrieval_debug, ingest, etc.).
    """

    model_config = ConfigDict(extra="allow")

    top_k_effective: Optional[int] = None
    analysis_intent: Optional[str] = None
    context_profile: Optional[str] = None

    retrieved_total: Optional[int] = None
    context_max_chars: Optional[int] = None
    context_used_chars: Optional[int] = None
    context_truncated: Optional[bool] = None

    fast_mode: Optional[bool] = None

    # Risk tier counts (additive)
    risk_objects: Optional[Dict[str, int]] = None

    # Optional rollup copy (additive)
    risk_summary: Optional[Dict[str, Any]] = None


# =============================================================================
# Response
# =============================================================================
class RagAnalyzeResponse(BaseModel):
    """
    Response for POST /api/rag/analyze

    IMPORTANT:
    - model_config.extra="allow" so adding new keys in backend won't break response validation.
    - Many fields are Optional to prevent “field missing -> validation failure -> dropped payload”.
    """

    model_config = ConfigDict(extra="allow")

    review_id: str

    # These are returned by service.py, but keep optional to tolerate older payloads
    mode: Optional[str] = None
    analysis_intent: Optional[str] = None
    context_profile: Optional[str] = None
    top_k: Optional[int] = None

    summary: str = Field(default="")

    citations: List[RagCitation] = Field(default_factory=list)
    retrieved_counts: Dict[str, int] = Field(default_factory=dict)

    sections: List[RagSection] = Field(default_factory=list)

    # Canonical risk list (may be empty)
    risks: List[Dict[str, Any]] = Field(default_factory=list)

    # Tier counts + rollup
    risk_objects: Dict[str, int] = Field(default_factory=dict)
    risk_summary: Dict[str, Any] = Field(default_factory=dict)

    # Dynamic risk grouping (what your UI needs)
    risk_areas_identified: List[str] = Field(default_factory=list)
    risk_areas: Dict[str, List[Dict[str, Any]]] = Field(default_factory=dict)

    # Stats remains untyped to preserve debug payload keys
    stats: Dict[str, Any] = Field(default_factory=dict)

    warnings: List[str] = Field(default_factory=list)

    # Debug-only (keep optional; backend may not return it)
    retrieved: Optional[Dict[str, Any]] = None