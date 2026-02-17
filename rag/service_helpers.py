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


def _safe_str(v: Any, max_len: int = 240) -> str:
    try:
        s = "" if v is None else str(v)
    except Exception:
        s = ""
    s = s.replace("\r", " ").replace("\n", " ").strip()
    if max_len > 0 and len(s) > max_len:
        s = s[: max_len - 3] + "..."
    return s


def retrieve_context(
    *,
    vector: Any,
    llm: Any,
    questions: List[str],
    effective_top_k: int,
    filters: Optional[Dict[str, Any]],
    snippet_cap: int,
    intent: str,
    profile: str,
    query_review_fn: Callable[..., List[Dict[str, Any]]],
    env_get_fn: Callable[[str, str], str],
    effective_context_chars_fn: Callable[[str], int],
    heuristic_hits: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Dict[str, List[Dict[str, Any]]], str, int, List[Dict[str, Any]]]:
    """
    Retrieval + context assembly + deterministic context capping.

    Returns:
      (retrieved_hits_by_question, context_string, max_context_chars_used_for_cap, signals)

    signals:
      Deterministic, non-contract-evidence hints (ex: heuristic hits). These are appended
      later by the caller (risk_triage only) with explicit markers so they are never cited
      as contract text.
    """

    retrieved: Dict[str, List[Dict[str, Any]]] = {}

    for q in questions:
        retrieved[q] = query_review_fn(
            vector=vector,
            llm=llm,
            question=q,
            top_k=effective_top_k,
            filters=filters,
        )

    def fmt_hit(h: Dict[str, Any]) -> str:
        meta = h.get("meta") or {}
        doc = meta.get("doc_name") or h.get("doc_name") or meta.get("doc_id") or "UnknownDoc"
        cs = meta.get("char_start")
        ce = meta.get("char_end")
        score = h.get("score")
        chunk_text = (h.get("chunk_text") or "").strip()
        snippet = chunk_text[:snippet_cap]
        return (
            "===BEGIN CONTRACT EVIDENCE===\n"
            f"DOC: {doc} | score={score} | span={cs}-{ce}\n"
            f"{snippet}\n"
            "===END CONTRACT EVIDENCE==="
        )

    blocks: List[str] = []
    for q in questions:
        hits = retrieved.get(q) or []
        blocks.append(
            f"QUESTION: {q}\nRETRIEVED EVIDENCE:\n"
            + "\n".join(fmt_hit(h) for h in hits[:effective_top_k])
        )

    context = "\n\n".join(blocks)

    # Context cap stack
    env_cap = int((env_get_fn("RAG_CONTEXT_MAX_CHARS", "16000") or "16000").strip() or "16000")
    hard_cap = int((env_get_fn("RAG_HARD_CONTEXT_MAX_CHARS", "80000") or "80000").strip() or "80000")
    profile_cap = int(effective_context_chars_fn(profile))

    if str(intent or "").strip().lower() == "risk_triage":
        max_chars = min(max(env_cap, profile_cap), hard_cap)
    else:
        max_chars = min(env_cap, profile_cap)

    context = context[:max_chars]

    # Strict summary gets an extra-tight context cap (keeps local CPU fast)
    if str(intent or "").strip().lower() != "risk_triage":
        try:
            strict_cap = int((env_get_fn("RAG_STRICT_CONTEXT_MAX_CHARS", "3500") or "3500").strip() or "3500")
        except Exception:
            strict_cap = 3500
        if strict_cap > 0 and len(context) > strict_cap:
            context = context[:strict_cap]

    # Deterministic signals (NOT contract evidence): heuristic hits only (for now)
    signals: List[Dict[str, Any]] = []
    try:
        # Cap signals count (prevent runaway)
        try:
            sig_cap = int((env_get_fn("RAG_SIGNALS_MAX_ITEMS", "40") or "40").strip() or "40")
        except Exception:
            sig_cap = 40

        for h in (heuristic_hits or []):
            if not isinstance(h, dict):
                continue

            hid = _safe_str(h.get("id") or h.get("hit_id") or h.get("key") or "")
            label = _safe_str(h.get("label") or h.get("name") or h.get("title") or h.get("rule") or "", max_len=200)
            severity = _safe_str(h.get("severity") or h.get("level") or h.get("risk") or "", max_len=40)
            why = _safe_str(h.get("why") or h.get("rationale") or h.get("reason") or "", max_len=220)

            if not (hid or label):
                continue

            signals.append(
                {
                    "id": hid or label,
                    "label": label or hid,
                    "severity": severity,
                    "source": "heuristic",
                    "why": why,
                }
            )

            if sig_cap > 0 and len(signals) >= sig_cap:
                break

    except Exception:
        signals = []

    return retrieved, context, max_chars, signals
