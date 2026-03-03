# --- FILE: rag/retrieval_engine.py ---
from typing import Any, Dict, List, Tuple


def _attach_evidence_id_to_hit(h: Dict[str, Any]) -> None:
    """Attach stable evidenceId = '{docId}::{chunk_id}' when possible.

    This is non-breaking: it only adds keys if docId + chunk_id can be determined.
    """
    try:
        if not isinstance(h, dict):
            return
        meta = h.get("meta") or {}
        doc_id = (
            meta.get("doc_id")
            or meta.get("docId")
            or meta.get("document_id")
            or meta.get("documentId")
            or h.get("doc_id")
            or h.get("docId")
            or h.get("document_id")
            or h.get("documentId")
            or ""
        )
        doc_id = str(doc_id or "").strip()
        cid = str(h.get("chunk_id") or h.get("chunkId") or meta.get("chunk_id") or meta.get("chunkId") or "").strip()
        if doc_id and cid:
            eid = f"{doc_id}::{cid}"
            # Canonical + legacy keys (carry forward)
            h.setdefault("evidenceId", eid)
            h.setdefault("evidence_id", eid)
            # Also provide docId/chunk_id canonical fields when missing
            h.setdefault("docId", doc_id)
            h.setdefault("doc_id", doc_id)
            h.setdefault("chunk_id", cid)
    except Exception:
        return


def effective_top_k(req_top_k: int, context_profile: str) -> int:
    # Deterministic clamp that respects request while preventing prompt truncation.
    p = (context_profile or "fast").strip().lower()
    k = int(req_top_k or 0)
    if k <= 0:
        k = 1

    # Profile ceilings (never exceed these)
    if p == "fast":
        cap = 2
    elif p == "deep":
        cap = 4
    else:
        # balanced/standard
        cap = 3

    return min(k, cap)


def effective_context_chars(context_profile: str) -> int:
    p = (context_profile or "fast").strip().lower()
    if p == "fast":
        return 16000
    if p == "deep":
        return 80000
    return 32000


def effective_snippet_chars(context_profile: str) -> int:
    p = (context_profile or "fast").strip().lower()
    # Conservative snippet caps (chars) to prevent prompt truncation.
    if p == "fast":
        return 200
    if p == "deep":
        return 320
    return 240


def retrieve_context_local(
    *,
    vector: Any,
    llm: Any,
    questions: List[str],
    review_id: str,
    effective_top_k: int,
    snippet_cap: int,
    context_cap: int,
    debug: bool,
) -> Tuple[Dict[str, List[Dict[str, Any]]], str, Dict[str, int], List[Dict[str, Any]]]:
    """
    Local retrieval helper (vector.query + llm.embed_texts).

    Returns:
      (retrieved_hits_by_question, assembled_context_str, retrieved_counts, retrieval_debug)
    """
    retrieved: Dict[str, List[Dict[str, Any]]] = {}
    retrieved_counts: Dict[str, int] = {}
    retrieval_debug: List[Dict[str, Any]] = []


    # Option 1: fallback query packs for sections that frequently return 0 hits due to wording mismatch.
    # These only execute when the primary question returns no hits.
    FALLBACK_QUERY_PACKS: Dict[str, List[str]] = {
        "Identify ambiguous/undefined terms and contradictions that require clarification.": [
            "ambiguity ambiguous undefined term conflict contradiction inconsistency precedence order of precedence",
            "in the event of conflict contract controls precedence hierarchy"
        ],
        "What are submission instructions and deadlines, including required formats and delivery method?": [
            "CDRL DID deliverable submission due date no later than within days government approval format template",
            "submit deliverables electronic copy government acceptance review comments resubmit"
        ],
        "What gaps require clarification from the Government?": [
            "to be determined TBD government will provide clarification",
            "missing information not specified requires clarification"
        ],
    }



    if not questions:
        return {}, "", {}, []

    if not hasattr(llm, "embed_texts"):
        raise RuntimeError("LLM provider does not implement embed_texts() required for retrieval")

    embs = llm.embed_texts(list(questions))
    if not isinstance(embs, list) or len(embs) != len(questions):
        raise RuntimeError("embed_texts returned unexpected embeddings count")

    for q, emb in zip(questions, embs):
        try:
            hits = vector.query(emb, top_k=effective_top_k, filters={"review_id": str(review_id)})
            # Attach stable evidence IDs to each hit (non-breaking)
            for _h in (hits or []):
                _attach_evidence_id_to_hit(_h)
        except Exception as e:
            hits = []
            if debug:
                retrieval_debug.append({"q": q, "error": repr(e)})

        # Fallback: if no hits for this question, try alternate queries (bounded).
        if not hits:
            used_fallback = False
            for fq in (FALLBACK_QUERY_PACKS.get(q) or []):
                try:
                    emb2s = llm.embed_texts([str(fq)])
                    emb2 = emb2s[0] if isinstance(emb2s, list) and emb2s else None
                    if emb2 is None:
                        continue
                    h2 = vector.query(emb2, top_k=min(effective_top_k, 4), filters={"review_id": str(review_id)})
                    for _h in (h2 or []):
                        _attach_evidence_id_to_hit(_h)
                    if h2:
                        hits = h2
                        used_fallback = True
                        break
                except Exception as _e2:
                    if debug:
                        retrieval_debug.append({"q": q, "fallback_q": fq, "fallback_error": repr(_e2)})
            if debug and used_fallback:
                retrieval_debug.append({"q": q, "fallback_used": True, "fallback_hits": int(len(hits or []))})

        retrieved[q] = hits or []
        retrieved_counts[q] = len(hits or [])

        if debug:
            retrieval_debug.append(
                {
                    "q": q,
                    "hits": len(hits or []),
                    "top": [
                        {
                            "doc_name": (h.get("doc_name") or ""),
                            "chunk_id": (h.get("chunk_id") or ""),
                            "evidenceId": (h.get("evidenceId") or h.get("evidence_id") or ""),
                            "score": h.get("score"),
                        }
                        for h in (hits or [])[:3]
                    ],
                }
            )

    ctx_parts: List[str] = []
    used = 0

    for q in questions:
        hits = retrieved.get(q) or []
        if not hits:
            continue

        hdr = f"Q: {q}\n"
        if used + len(hdr) > context_cap:
            break
        ctx_parts.append(hdr)
        used += len(hdr)

        per_q = min(max(effective_top_k, 8), 20)

        # If question count is high, treat as triage-like context pressure.
        if str(review_id) and isinstance(questions, list) and len(questions) >= 15:
            per_q = min(max(effective_top_k, 4), 8)

        for h in hits[:per_q]:
            txt = (h.get("chunk_text") or "").strip()
            if not txt:
                continue
            if snippet_cap > 0 and len(txt) > snippet_cap:
                txt = txt[:snippet_cap].rstrip() + "..."

            meta = h.get("meta") or {}
            doc = meta.get("doc_name") or h.get("doc_name") or meta.get("doc_id") or h.get("document_id") or "doc"
            cid = h.get("chunk_id") or ""
            line = f"- ({doc} / {cid}) {txt}\n"
            if used + len(line) > context_cap:
                break
            ctx_parts.append(line)
            used += len(line)

        ctx_parts.append("\n")
        used += 1
        if used >= context_cap:
            break

    context = "".join(ctx_parts).strip()
    return retrieved, context, retrieved_counts, retrieval_debug


def retrieve_context_local_by_section(
    *,
    vector: Any,
    llm: Any,
    section_query_packs: Dict[str, List[str]],
    review_id: str,
    effective_top_k: int,
    snippet_cap: int,
    context_cap: int,
    debug: bool,
) -> Tuple[Dict[str, List[Dict[str, Any]]], str, Dict[str, int], List[Dict[str, Any]]]:
    """
    Section-scoped retrieval helper:
      - run 2-4 query variants per section
      - union hits per section
      - dedupe by evidenceId
      - return context grouped by section_id
    """
    retrieved_by_section: Dict[str, List[Dict[str, Any]]] = {}
    retrieved_counts: Dict[str, int] = {}
    retrieval_debug: List[Dict[str, Any]] = []

    if not section_query_packs:
        return {}, "", {}, []

    if not hasattr(llm, "embed_texts"):
        raise RuntimeError("LLM provider does not implement embed_texts() required for retrieval")

    # Per-section caps: keep bounded to avoid prompt blow-ups.
    per_section_cap = int(max(2, min(max(effective_top_k, 4), 8)))

    for sid, qpack in (section_query_packs or {}).items():
        if not isinstance(qpack, list) or not qpack:
            retrieved_by_section[sid] = []
            retrieved_counts[sid] = 0
            continue

        # Embed all queries for this section at once
        embs = llm.embed_texts([str(q) for q in qpack])
        if not isinstance(embs, list) or len(embs) != len(qpack):
            raise RuntimeError("embed_texts returned unexpected embeddings count (section mode)")

        best_by_eid: Dict[str, Dict[str, Any]] = {}

        for q, emb in zip(qpack, embs):
            hits: List[Dict[str, Any]] = []
            try:
                hits = vector.query(emb, top_k=effective_top_k, filters={"review_id": str(review_id)}) or []
                for _h in hits:
                    _attach_evidence_id_to_hit(_h)

                    # NORMALIZE_HIT_TEXT_FIELDS_FOR_MULTIPASS
                    # Ensure downstream (multipass + evidence attach) can always read evidence text.
                    try:
                        if isinstance(_h, dict):
                            t = (_h.get("chunk_text") or _h.get("snippet") or _h.get("text") or _h.get("content") or _h.get("passage") or "")
                            if isinstance(t, str) and t.strip():
                                if not (_h.get("chunk_text") or "").strip():
                                    _h["chunk_text"] = t
                                if not (_h.get("snippet") or "").strip():
                                    _h["snippet"] = t
                    except Exception:
                        pass
            except Exception as e:
                if debug:
                    retrieval_debug.append({"section": sid, "q": q, "error": repr(e)})
                hits = []

            # Dedup by evidenceId, keep highest score
            for h in hits:
                eid = str(h.get("evidenceId") or h.get("evidence_id") or "").strip()
                if not eid:
                    continue
                prev = best_by_eid.get(eid)
                if prev is None:
                    best_by_eid[eid] = h
                else:
                    try:
                        if (h.get("score") or 0) > (prev.get("score") or 0):
                            best_by_eid[eid] = h
                    except Exception:
                        # if score missing, keep first
                        pass

            if debug:
                retrieval_debug.append(
                    {
                        "section": sid,
                        "q": q,
                        "hits": int(len(hits)),
                        "top": [
                            {
                                "doc_name": (hh.get("doc_name") or ""),
                                "chunk_id": (hh.get("chunk_id") or ""),
                                "evidenceId": (hh.get("evidenceId") or hh.get("evidence_id") or ""),
                                "score": hh.get("score"),
                            }
                            for hh in hits[:3]
                        ],
                    }
                )

        # Final hits for section: score desc, cap
        final_hits = list(best_by_eid.values())
        final_hits.sort(key=lambda x: (x.get("score") or 0), reverse=True)
        final_hits = final_hits[:per_section_cap]

        # section_relevance_filter: reduce cross-section evidence bleed
        try:
            _sid = str(sid or '').strip().lower()
            _ban = []
            # Keep SECURITY unconstrained; it should include IL5/RMF/prohibited/logging/etc.
            if _sid in ('mission-objective','scope-of-work','deliverables-timelines'):
                _ban = ['prohibited actions','il5','rmf','ato','cmmc','dfars','encryption','logging','audit']
            elif _sid in ('financial-risks',):
                _ban = ['prohibited actions','il5','rmf','ato','cmmc','dfars','encryption','logging','audit','unlimited rights','data rights']
            elif _sid in ('eligibility-personnel-constraints',):
                _ban = ['unlimited rights','data rights','dfars 252.227','license','qasp','surveillance']
            elif _sid in ('legal-data-rights-risks',):
                _ban = ['prohibited actions','il5','rmf','ato','cmmc','logging','encryption','qasp','surveillance']
            elif _sid in ('submission-instructions-deadlines',):
                _ban = ['prohibited actions','il5','rmf','ato','cmmc','logging','encryption','unlimited rights','data rights']
            elif _sid in ('contradictions-inconsistencies',):
                _ban = ['prohibited actions','il5','rmf','ato','cmmc','logging','encryption']

            if _ban and isinstance(final_hits, list) and final_hits:
                _kept = []
                for _h in final_hits:
                    try:
                        _t = str(_h.get('chunk_text') or _h.get('snippet') or _h.get('text') or '').lower()
                        if any(b in _t for b in _ban):
                            continue
                    except Exception:
                        pass
                    _kept.append(_h)
                if _kept:
                    final_hits = _kept
        except Exception:
            pass

        retrieved_by_section[sid] = final_hits
        retrieved_counts[sid] = int(len(final_hits))

        if debug:
            retrieval_debug.append({"section": sid, "union_hits": int(len(best_by_eid)), "final_hits": int(len(final_hits))})

    # Assemble context by section
    ctx_parts: List[str] = []
    used = 0

    for sid, hits in retrieved_by_section.items():
        if not hits:
            continue

        hdr = f"SECTION: {sid}\n"
        if used + len(hdr) > context_cap:
            break
        ctx_parts.append(hdr)
        used += len(hdr)

        for h in hits:
            txt = (h.get("chunk_text") or "").strip()
            if not txt:
                continue
            if snippet_cap > 0 and len(txt) > snippet_cap:
                txt = txt[:snippet_cap].rstrip() + "..."

            meta = h.get("meta") or {}
            doc = meta.get("doc_name") or h.get("doc_name") or meta.get("doc_id") or h.get("document_id") or "doc"
            cid = h.get("chunk_id") or ""
            eid = h.get("evidenceId") or h.get("evidence_id") or ""

            line = f"- ({doc} / {cid} / {eid}) {txt}\n"
            if used + len(line) > context_cap:
                break
            ctx_parts.append(line)
            used += len(line)

        ctx_parts.append("\n")
        used += 1
        if used >= context_cap:
            break

    context = "".join(ctx_parts).strip()
    return retrieved_by_section, context, retrieved_counts, retrieval_debug


