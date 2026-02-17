from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# NOTE: This module intentionally contains pure helper functions used by rag.service.
# Keep imports minimal to avoid circular dependencies.


def build_rag_response_dict(
    *,
    review_id: str,
    mode: str,
    effective_top_k: int,
    intent: str,
    context_profile: str,
    summary: str,
    citations: List[Dict[str, Any]],
    retrieved_counts: Dict[str, int],
    risks: List[Dict[str, Any]],
    sections: List[Dict[str, Any]],
    stats: Optional[Dict[str, Any]],
    warnings: List[str],
) -> Dict[str, Any]:
    return {
        "review_id": str(review_id),
        "mode": str(mode),
        "top_k": int(effective_top_k),
        "analysis_intent": str(intent),
        "context_profile": str(context_profile),
        "summary": summary or "",
        "citations": citations or [],
        "retrieved_counts": retrieved_counts or {},
        "risks": risks or [],
        "sections": sections or [],
        "stats": stats,
        "warnings": warnings or [],
    }
