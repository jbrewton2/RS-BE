from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple


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


def materialize_risk_register(
    *,
    storage: Any,
    review_id: str,
    intent: str,
    parsed_sections: List[Dict[str, Any]],
    heuristic_hits: Optional[List[Dict[str, Any]]],
    enable_inference_risks: bool,
    inference_candidates: Optional[List[str]],
    # injected callables (avoid circular imports)
    read_reviews_fn: Callable[[Any], List[Dict[str, Any]]],
    materialize_flags_fn: Callable[[Dict[str, Any]], List[Dict[str, Any]]],
    materialize_heuristics_fn: Callable[[Optional[List[Dict[str, Any]]]], List[Dict[str, Any]]],
    materialize_sections_fn: Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]],
    materialize_inference_fn: Callable[..., List[Dict[str, Any]]],
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """
    Canonical deterministic risk register materialization + merge/dedupe.

    Tier order:
      - Tier 3: flags (highest confidence)
      - Tier 2: heuristics
      - Tier 2: section-derived (triage only)
      - Tier 1: inference (lowest confidence, optional)

    Returns:
      (merged_risks, counts_dict)
    """

    # 1) Flags -> risks (Tier 3)
    try:
        reviews = read_reviews_fn(storage)
        review = next((r for r in (reviews or []) if str(r.get("id")) == str(review_id)), None) or {}
        risks_flags = materialize_flags_fn(review)
    except Exception:
        risks_flags = []

    # 2) Heuristic hits -> risks (Tier 2)
    try:
        risks_heur = materialize_heuristics_fn(heuristic_hits)
    except Exception:
        risks_heur = []

    # 3) Section-derived risks -> risks (Tier 2, triage only)
    if str(intent or "").strip().lower() == "risk_triage":
        try:
            risks_det = materialize_sections_fn(parsed_sections or [])
        except Exception:
            risks_det = []
    else:
        risks_det = []

    # 4) Inference risks (Tier 1)
    try:
        risks_inf = materialize_inference_fn(
            parsed_sections or [],
            enable_inference_risks=enable_inference_risks,
            inference_candidates=inference_candidates,
        )
    except Exception:
        risks_inf = []

    merged: List[Dict[str, Any]] = []
    seen_ids = set()

    def add_all(src: List[Dict[str, Any]]):
        for r in (src or []):
            if not isinstance(r, dict):
                continue
            rid = str(r.get("id") or r.get("risk_id") or "").strip()
            if not rid:
                continue
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            merged.append(r)

    # preserve tier priority
    add_all(risks_flags)
    add_all(risks_heur)
    add_all(risks_det)
    add_all(risks_inf)

    counts = {
        "tier3_flags": int(len(risks_flags or [])),
        "tier2_heuristics": int(len(risks_heur or [])),
        "tier2_sections": int(len(risks_det or [])),
        "tier1_inference": int(len(risks_inf or [])),
        "total": int(len(merged)),
    }

    return merged, counts
