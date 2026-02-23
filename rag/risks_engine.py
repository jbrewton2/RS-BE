from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from rag.service_helpers import derive_section_risks


def build_risks_and_tier_counts(
    *,
    intent: str,
    review: Dict[str, Any],
    heuristic_hits: Optional[List[Dict[str, Any]]],
    sections: List[Dict[str, Any]],
    inference_candidates: Optional[List[str]],
    norm_sev_fn: Callable[[str], str],
    max_heuristics: int = 25,
    max_inference: int = 10,
    section_max_items: int = 25,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """
    Canonical risk materialization logic used by rag/service.py.

    Tier order:
      - Tier 3: autoFlag
      - Tier 2: heuristic
      - Tier 2: sectionDerived (deterministic)
      - Tier 1: ai_only (inference) REQUIRED

    NOTE:
      Tier1 "required" means we do not gate this behind feature flags. If inference_candidates
      is empty/None, Tier1 count may be 0 (caller can generate candidates via multi-pass).
    """
    risks: List[Dict[str, Any]] = []

    tier_counts = {
        "tier3_flags": 0,
        "tier2_heuristics": 0,
        "tier2_sections": 0,
        "tier1_inference": 0,
        "total": 0,
    }

    if intent != "risk_triage":
        return risks, tier_counts

    # --- Tier 3: flags ---
    try:
        af = (review or {}).get("autoFlags") or {}
        hits = af.get("hits") or []
        if isinstance(hits, list) and hits:
            for i, h in enumerate(hits):
                if not isinstance(h, dict):
                    continue
                lbl = str(h.get("label") or h.get("name") or h.get("id") or "").strip()
                if not lbl:
                    continue
                rid = str(h.get("hit_key") or h.get("key") or h.get("id") or f"autoflag:{lbl}:{i}").strip()
                sev = norm_sev_fn(str(h.get("severity") or "Low"))
                risks.append({"id": rid, "label": lbl, "severity": sev, "source": "autoFlag"})
    except Exception:
        pass

    # --- Tier 2: heuristics ---
    try:
        if isinstance(heuristic_hits, list) and heuristic_hits:
            for i, h in enumerate(heuristic_hits[: max_heuristics]):
                if not isinstance(h, dict):
                    continue
                lbl = str(h.get("label") or h.get("name") or h.get("id") or "").strip()
                if not lbl:
                    continue
                rid = str(h.get("id") or f"heur:{lbl}:{i}").strip()
                sev = norm_sev_fn(str(h.get("severity") or "Low"))
                risks.append({"id": rid, "label": lbl, "severity": sev, "source": "heuristic"})
    except Exception:
        pass

    # --- Tier 2: section-derived (deterministic) ---
    try:
        sec_risks = derive_section_risks(
            sections or [],
            max_items=int(section_max_items),
            enable_ambiguity=True,
            enable_missing_evidence=True,
        )
        if isinstance(sec_risks, list) and sec_risks:
            risks.extend(sec_risks)
    except Exception:
        pass

    # --- Tier 1: inference REQUIRED (ai_only) ---
    try:
        if isinstance(inference_candidates, list) and inference_candidates:
            for i, c in enumerate(inference_candidates[: max_inference]):
                t = str(c or "").strip()
                if not t:
                    continue
                rid = f"ai:{i}:{t[:40]}"
                risks.append(
                    {
                        "id": rid,
                        "label": t,
                        "severity": "Low",
                        "source": "ai_only",
                        "category": "ai_identified_risk",
                        "confidence": 0.25,
                    }
                )
    except Exception:
        pass

    # --- Tier counts ---
    try:
        tier_counts["tier3_flags"] = int(len([r for r in (risks or []) if r.get("source") == "autoFlag"]))
        tier_counts["tier2_heuristics"] = int(len([r for r in (risks or []) if r.get("source") == "heuristic"]))
        tier_counts["tier2_sections"] = int(len([r for r in (risks or []) if r.get("source") == "sectionDerived"]))
        tier_counts["tier1_inference"] = int(len([r for r in (risks or []) if r.get("source") == "ai_only"]))
        tier_counts["total"] = int(len(risks or []))
    except Exception:
        tier_counts["total"] = int(len(risks or []))

    return risks, tier_counts
