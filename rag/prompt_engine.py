from __future__ import annotations

from typing import Any, Dict, List, Optional

# Prompt-related helpers split from rag/service.py
# Keep provider calls OUT of here. Only prompt assembly / formatting.


def render_deterministic_signals_block(
    *,
    review: Dict[str, Any],
    heuristic_hits: Optional[List[Dict[str, Any]]],
    enable_inference_risks: bool,
    inference_candidates: Optional[List[str]],
) -> str:
    """
    Render deterministic signals block for risk_triage prompt context.

    NOTE: Tier1 is required going forward, but this function only renders the
    signals block (NOT the risk register). Risk materialization happens elsewhere.
    """
    # This is a placeholder until we copy the exact runtime function body over.
    # We will replace this body with the extracted function from service.py next.
    return ""

def render_deterministic_signals_block(
    *,
    review: Dict[str, Any],
    heuristic_hits: Optional[List[Dict[str, Any]]] = None,
    enable_inference_risks: bool = True,
    inference_candidates: Optional[List[str]] = None,
) -> str:
    """
    NOT contract evidence. Used only for triage prioritization.
    Tests require markers and the phrase 'NOT CONTRACT EVIDENCE' to be visible in debug_context.
    """
    parts: List[str] = []

    # autoFlags (deterministic)
    af = (review or {}).get("autoFlags") or {}
    hits = af.get("hits") or []
    if isinstance(hits, list) and hits:
        parts.append("AUTOFLAGS (deterministic hits)")
        for h in hits[:25]:
            if not isinstance(h, dict):
                continue
            label = str(h.get("label") or h.get("name") or h.get("id") or "").strip()
            if not label:
                continue
            sev = str(h.get("severity") or "").strip() or "High"
            key = str(h.get("hit_key") or h.get("key") or h.get("id") or "").strip()
            line = f"- {label} (src=autoFlag, severity={sev}"
            if key:
                line += f", key={key}"
            line += ")"
            parts.append(line)

    # heuristic hits
    if isinstance(heuristic_hits, list) and heuristic_hits:
        parts.append("")
        parts.append("HEURISTIC HITS (semi-deterministic)")
        for h in heuristic_hits[:25]:
            if not isinstance(h, dict):
                continue
            label = str(h.get("label") or h.get("name") or h.get("id") or "").strip()
            if not label:
                continue
            parts.append(f"- {label} (src=heuristic)")

    # inference candidates (lowest confidence)
    if enable_inference_risks and isinstance(inference_candidates, list) and inference_candidates:
        parts.append("")
        parts.append("INFERENCE CANDIDATES (LLM suggestions; lowest confidence)")
        for c in inference_candidates[:25]:
            t = str(c or "").strip()
            if t:
                parts.append(f"- {t}")

    block = "\n".join(parts).strip()
    if not block:
        return ""

    return "BEGIN DETERMINISTIC SIGNALS\nNOT CONTRACT EVIDENCE\n" + block + "\nEND DETERMINISTIC SIGNALS"


# Deterministic retrieval
# =============================================================================

