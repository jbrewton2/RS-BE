from __future__ import annotations


from rag.risk_taxonomy import detect_triggered_areas_from_signals, build_targeted_questions
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
      - Tier 1: inference (lowest confidence, REQUIRED)

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
            enable_inference_risks=True,
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


def derive_section_risks(
    parsed_sections: List[Dict[str, Any]],
    *,
    max_items: int = 25,
    enable_ambiguity: bool = True,
    enable_missing_evidence: bool = True,
) -> List[Dict[str, Any]]:
    """
    Deterministic Tier-2 section-derived risks.

    This MUST NOT call the LLM or providers. It only inspects parsed sections (and any evidence
    that has already been attached to them upstream).
    """
    risks: List[Dict[str, Any]] = []
    seen = set()

    ambiguity_terms = [
        "may",
        "should",
        "as appropriate",
        "as needed",
        "best effort",
        "endeavor",
        "where practicable",
        "where practical",
        "as agreed",
        "at its discretion",
    ]

    def _sec_title(s: Dict[str, Any]) -> str:
        return _safe_str(s.get("title") or s.get("header") or s.get("name") or "" , max_len=120)

    def _sec_text(s: Dict[str, Any]) -> str:
        # tolerate different shapes
        return _safe_str(
            s.get("text") or s.get("content") or s.get("body") or s.get("summary") or "",
            max_len=200000,
        ).lower()

    def _sec_evidence_count(s: Dict[str, Any]) -> int:
        # Evidence is usually attached upstream. Try common keys.
        ev = s.get("evidence") or s.get("citations") or s.get("evidence_blocks") or s.get("evidenceItems")
        if isinstance(ev, list):
            return int(len(ev))
        return 0

    def _add(r: Dict[str, Any]):
        rid = str(r.get("id") or "").strip()
        if not rid or rid in seen:
            return
        seen.add(rid)
        risks.append(r)

    for sec in (parsed_sections or []):
        if not isinstance(sec, dict):
            continue
        title = _sec_title(sec)
        text = _sec_text(sec)
        evc = _sec_evidence_count(sec)

        # --- Rule: ambiguity language in obligations ---
        if enable_ambiguity and text:
            for term in ambiguity_terms:
                if term in text:
                    slug = "".join([c.lower() for c in title if c.isalnum()])[:32] or "unknown"
                    rid = f"sec_ambiguous_{slug}_{term.replace(' ','_')}"
                    _add(
                        {
                            "id": rid,
                            "label": f"Ambiguous obligation language in section: {title}",
                            "severity": "Medium",
                            "source": "sectionDerived",
                            "category": "project_level",
                            "why": f"Found ambiguity term '{term}' in section text (deterministic rule).",
                        }
                    )
                    break  # one ambiguity risk per section

        # --- Rule: no evidence attached to this section (retrieval starvation / mapping issue) ---
        if enable_missing_evidence:
            # Avoid spamming OVERVIEW if you prefer; keep it for now as a low-sev operator warning.
            if evc <= 0:
                slug = "".join([c.lower() for c in title if c.isalnum()])[:32] or "unknown"
                rid = f"sec_no_evidence_{slug}"
                _add(
                    {
                        "id": rid,
                        "label": f"No contract evidence attached for section: {title}",
                        "severity": "Low",
                        "source": "sectionDerived",
                        "category": "project_level",
                        "why": "Section has zero attached evidence items; may indicate retrieval starvation or mapping gap.",
                    }
                )

        if max_items > 0 and len(risks) >= max_items:
            break

    return risks

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
    query_review_fn: Optional[Callable[..., List[Dict[str, Any]]]] = None,
    env_get_fn: Callable[[str, str], str],
    effective_context_chars_fn: Callable[[str], int],
    heuristic_hits: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Dict[str, List[Dict[str, Any]]], str, int, List[Dict[str, Any]]]:
    """
    Retrieval + context assembly + deterministic context capping.

    Returns:
      (retrieved_hits_by_question, context_string, max_context_chars_used_for_cap, signals)

    IMPORTANT:
      Keep BOTH formats per hit:
        1) BEGIN/END blocks (back-compat for older parsers)
        2) Parseable EVIDENCE line (for _EVIDENCE_LINE_RE in rag/service.py):
             EVIDENCE: <snippet> (Doc: <doc> span: <cs>-<ce>)
    """

    # --- resolve query function (back-compat) ---
    def _resolve_query_fn() -> Callable[..., List[Dict[str, Any]]]:
        if callable(query_review_fn):
            return query_review_fn  # user-provided

        # Prefer vector.query_review if present (most explicit)
        cand = getattr(vector, "query_review", None)
        if callable(cand):
            return cand

        # Common alternatives
        cand = getattr(vector, "query", None)
        if callable(cand):
            return cand

        cand = getattr(vector, "search", None)
        if callable(cand):
            return cand

        raise TypeError(
            "retrieve_context: no query function available. "
            "Provide query_review_fn=... or implement vector.query_review / vector.query / vector.search."
        )

    qfn = _resolve_query_fn()

    def _call_query_fn(*, question: str) -> List[Dict[str, Any]]:
        """
        Tolerant wrapper because different vector stores use different parameter names.
        Tries a few common calling conventions in order.
        """
        # 1) Preferred internal convention used by earlier code paths
        try:
            return (
                qfn(
                    vector=vector,
                    llm=llm,
                    question=question,
                    top_k=effective_top_k,
                    filters=filters,
                )
                or []
            )
        except TypeError:
            pass

        # 2) Common convention: qfn(question=..., top_k=..., filters=...)
        try:
            return (
                qfn(
                    question=question,
                    top_k=effective_top_k,
                    filters=filters,
                )
                or []
            )
        except TypeError:
            pass

        # 3) Common convention: qfn(query=..., k=..., filters=...)
        try:
            return (
                qfn(
                    query=question,
                    k=effective_top_k,
                    filters=filters,
                )
                or []
            )
        except TypeError:
            pass

        # 4) Minimal: qfn(question)
        try:
            return qfn(question) or []
        except TypeError:
            # 5) Minimal: qfn(query)
            return qfn(question) or []

    retrieved: Dict[str, List[Dict[str, Any]]] = {}

    # ---- retrieve per question ----
    for q in questions:
        retrieved[q] = _call_query_fn(question=q)

    def _fmt_ev_block(h: Dict[str, Any]) -> str:
        meta = h.get("meta") or {}

        doc = meta.get("doc_name") or h.get("doc_name") or meta.get("doc_id") or "UnknownDoc"
        cs = meta.get("char_start")
        ce = meta.get("char_end")
        score = h.get("score")

        cs_s = "0" if cs is None else str(cs)
        ce_s = "0" if ce is None else str(ce)

        chunk_text = (h.get("chunk_text") or "").strip()
        snippet = chunk_text[: max(0, int(snippet_cap or 0))]

        # back-compat markers + parseable EVIDENCE line
        return (
            "===BEGIN CONTRACT EVIDENCE===\n"
            f"DOC: {doc} | score={score} | span={cs_s}-{ce_s}\n"
            f"EVIDENCE: {snippet} (Doc: {doc} span: {cs_s}-{ce_s})\n"
            "===END CONTRACT EVIDENCE==="

        )

    # ---- build context blocks deterministically ----
    blocks: List[str] = []
    for q in questions:
        hits = (retrieved.get(q) or [])[: int(effective_top_k or 0)]
        ev_blocks = "\n".join(_fmt_ev_block(h) for h in hits if isinstance(h, dict))
        if not ev_blocks:
            ev_blocks = "(no hits)"
        blocks.append(f"QUESTION: {q}\nRETRIEVED EVIDENCE:\n{ev_blocks}")

    context = "\n\n".join(blocks)

    # ---- context cap stack ----
    try:
        env_cap = int((env_get_fn("RAG_CONTEXT_MAX_CHARS", "16000") or "16000").strip() or "16000")
    except Exception:
        env_cap = 16000

    try:
        hard_cap = int((env_get_fn("RAG_HARD_CONTEXT_MAX_CHARS", "80000") or "80000").strip() or "80000")
    except Exception:
        hard_cap = 80000

    try:
        profile_cap = int(effective_context_chars_fn(profile))
    except Exception:
        profile_cap = env_cap

    if str(intent or "").strip().lower() == "risk_triage":
        max_chars = min(max(env_cap, profile_cap), hard_cap)
    else:
        max_chars = min(env_cap, profile_cap)

    context = context[:max_chars]

    # Strict summary gets an extra-tight cap
    if str(intent or "").strip().lower() != "risk_triage":
        try:
            strict_cap = int((env_get_fn("RAG_STRICT_CONTEXT_MAX_CHARS", "3500") or "3500").strip() or "3500")
        except Exception:
            strict_cap = 3500
        if strict_cap > 0 and len(context) > strict_cap:
            context = context[:strict_cap]

    # ---- deterministic signals (NOT contract evidence) ----
    signals: List[Dict[str, Any]] = []
    try:
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
    return retrieved, context, max_chars, signals

def _extend_questions_with_targeted(
    base_questions: list[str],
    intent: str,
    auto_flags: dict | None,
    heuristic_hits: list[dict] | None,
    max_targeted: int = 10,
) -> list[str]:
    """
    Adds targeted questions for risk areas triggered by deterministic signals.
    - Triggers come from Tier 3 flags (review.autoFlags.hits) and Tier 2 heuristic_hits.
    - Only applies for risk_triage.
    """
    intent_l = str(intent or "").strip().lower()
    if intent_l != "risk_triage":
        return base_questions or []

    flag_hits = []
    if isinstance(auto_flags, dict):
        flag_hits = auto_flags.get("hits") or []

    triggered = detect_triggered_areas_from_signals(flag_hits=flag_hits, heuristic_hits=heuristic_hits or [])
    targeted = build_targeted_questions(triggered, max_questions=max_targeted)

    # Deterministic ordering: base questions first, then targeted (dedup).
    out: list[str] = []
    seen: set[str] = set()

    for q in (base_questions or []):
        qs = str(q).strip()
        if not qs or qs in seen:
            continue
        out.append(qs); seen.add(qs)

    for q in targeted:
        qs = str(q).strip()
        if not qs or qs in seen:
            continue
        out.append(qs); seen.add(qs)

    return out



