# backend/reviews/router.py
from __future__ import annotations

from typing import List, Dict, Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Depends

from auth.jwt import get_current_user
from core.deps import StorageDep  # still used for pdf/text pointers later
from core.dynamo_meta import DynamoMeta
from flags.service import scan_text_for_flags

router = APIRouter(
    prefix="/reviews",
    tags=["reviews"],
    dependencies=[Depends(get_current_user)],
)


def _read_reviews_file(storage=None) -> List[Dict[str, Any]]:
    """
    COMPAT SHIM for older callers (ex: flags/router.py) that imported _read_reviews_file.
    Returns a list of review META rows from Dynamo.
    """
    meta = DynamoMeta()
    return meta.list_reviews()


def _build_hit_key(doc_id: str, flag_id: str, line: int, index: int) -> str:
    return f"{doc_id}:{flag_id}:{line}:{index}"


def _attach_auto_flags_to_review(review: Dict[str, Any]) -> Dict[str, Any]:
    docs = review.get("docs") or []
    per_doc_hits: List[dict] = []
    hits_by_doc: Dict[str, List[dict]] = {}

    full_text_parts: List[str] = []

    for doc in docs:
        doc_id = doc.get("id")
        if not doc_id:
            continue

        doc_name = doc.get("name") or doc.get("filename") or "Document"
        raw = doc.get("content") or doc.get("text") or ""
        text = (raw or "").strip()
        if not text:
            hits_by_doc[doc_id] = []
            continue

        full_text_parts.append(text)

        scan_result = scan_text_for_flags(text, record_usage=False)
        doc_hits: List[dict] = []

        for idx, h in enumerate(scan_result.get("hits") or []):
            flag_id = h.get("id") or h.get("flagId") or h.get("label") or "flag"
            line = h.get("line") or 0
            match_text = (h.get("match") or "")[:240]

            enriched = dict(h)
            enriched["docId"] = doc_id
            enriched["docName"] = doc_name
            enriched["snippet"] = match_text
            enriched["hit_key"] = _build_hit_key(doc_id, flag_id, line, idx)

            doc_hits.append(enriched)
            per_doc_hits.append(enriched)

        hits_by_doc[doc_id] = doc_hits

    full_text = "\n".join(full_text_parts).strip()
    if full_text:
        combined = scan_text_for_flags(full_text, record_usage=True)
        summary = combined.get("summary")
    else:
        summary = None

    review["autoFlags"] = {
        "hits": per_doc_hits,
        "summary": summary,
        "hitsByDoc": hits_by_doc,
        "explainReady": bool(summary),
    }
    return review


def _ensure_id_contract(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    Frontend expects `id`. Dynamo meta uses `review_id`.
    We support both, but we ALWAYS return `id` in API responses.
    """
    if not isinstance(item, dict):
        return item
    rid = item.get("id") or item.get("review_id")
    if rid:
        item["id"] = rid
        item["review_id"] = rid
    return item



def _backfill_evidence_provenance(ev: Dict[str, Any], docs: list[dict]) -> Dict[str, Any]:
    """
    Safe-only evidence provenance backfill.

    Rules (deterministic, no fuzzy searching):
      1) Parse evidenceId formats:
         - "{docId}::{page}:{charStart}:{charEnd}"
         - "{docId}::{chunkId}" where chunkId looks like "{page}:{charStart}:{charEnd}"
      2) If doc filename is present and spans exist but docId missing, map via docs[].
      3) Never attempt text search to guess spans.
    """
    if not isinstance(ev, dict):
        return ev

    def _to_int(x):
        try:
            if x is None:
                return None
            if isinstance(x, bool):
                return None
            s = str(x).strip()
            if not s:
                return None
            return int(s)
        except Exception:
            return None

    # Existing fields (normalize key variants)
    doc_id = (ev.get("docId") or ev.get("doc_id") or "").strip()
    doc_name = (ev.get("doc") or ev.get("document_name") or ev.get("documentName") or "").strip()
    cs = _to_int(ev.get("charStart") if ev.get("charStart") is not None else ev.get("char_start"))
    ce = _to_int(ev.get("charEnd") if ev.get("charEnd") is not None else ev.get("char_end"))
    evidence_id = (ev.get("evidenceId") or ev.get("evidence_id") or "").strip()

    # Build doc-name -> doc_id map from docs[]
    name_to_id = {}
    if isinstance(docs, list):
        for d in docs:
            if not isinstance(d, dict):
                continue
            did = (d.get("doc_id") or d.get("id") or "").strip()
            fname = (d.get("filename") or d.get("name") or "").strip()
            if did and fname:
                name_to_id[fname] = did

    # 1) Parse evidenceId: "{docId}::{page}:{cs}:{ce}"
    if evidence_id and ("::" in evidence_id):
        left, right = evidence_id.split("::", 1)
        left = left.strip()
        right = right.strip()

        # If left looks like a UUID-ish doc id, treat as docId
        if left and not doc_id:
            doc_id = left

        # Try parse right as page:cs:ce OR cs:ce (we accept both)
        parts = [p.strip() for p in right.split(":") if p.strip()]
        if len(parts) >= 3:
            # common: page, cs, ce
            maybe_cs = _to_int(parts[-2])
            maybe_ce = _to_int(parts[-1])
            if cs is None and maybe_cs is not None:
                cs = maybe_cs
            if ce is None and maybe_ce is not None:
                ce = maybe_ce
        elif len(parts) == 2:
            maybe_cs = _to_int(parts[0])
            maybe_ce = _to_int(parts[1])
            if cs is None and maybe_cs is not None:
                cs = maybe_cs
            if ce is None and maybe_ce is not None:
                ce = maybe_ce

    # 2) If spans exist but docId missing, map doc filename -> doc_id using docs[]
    if (not doc_id) and doc_name and (cs is not None) and (ce is not None):
        did = name_to_id.get(doc_name)
        if did:
            doc_id = did

    # Write back normalized fields (only if we have them)
    if doc_id:
        ev["docId"] = doc_id
    if cs is not None:
        ev["charStart"] = cs
    if ce is not None:
        ev["charEnd"] = ce
    if evidence_id:
        ev["evidenceId"] = evidence_id

    return ev


def _backfill_aiRisks_evidence(item: Dict[str, Any]) -> None:
    """
    Apply provenance backfill to aiRisks[*].evidence[*] using item['docs'] mapping when present.
    """
    if not isinstance(item, dict):
        return
    docs = item.get("docs") or []
    risks = item.get("aiRisks") or []
    if not isinstance(risks, list):
        return
    for r in risks:
        if not isinstance(r, dict):
            continue
        evs = r.get("evidence") or []
        if not isinstance(evs, list):
            continue
        for i in range(len(evs)):
            if isinstance(evs[i], dict):
                evs[i] = _backfill_evidence_provenance(evs[i], docs)
        r["evidence"] = evs


def _is_traceable_evidence(ev: Dict[str, Any]) -> bool:
    if not isinstance(ev, dict):
        return False
    doc_id = str(ev.get("docId") or ev.get("doc_id") or "").strip()
    cs = ev.get("charStart") if ev.get("charStart") is not None else ev.get("char_start")
    ce = ev.get("charEnd") if ev.get("charEnd") is not None else ev.get("char_end")
    return bool(doc_id) and (cs is not None) and (ce is not None)

def _drop_untraceable_evidence(evs: Any) -> list:
    if not isinstance(evs, list):
        return []
    out = []
    for e in evs:
        if _is_traceable_evidence(e):
            out.append(e)
    return out

def _backfill_aiRisks_from_sections(item: Dict[str, Any]) -> None:
    """
    Deterministic join: RAG_SECTION aiRisks inherit evidence from matching section outputs.

    Evidence must never float:
      - If aiRisk evidence is missing/untraceable, replace it with traceable section evidence.
      - After replacement, drop any remaining untraceable evidence items.
    """
    if not isinstance(item, dict):
        return

    # Locate sections list (prefer item['rag']['sections'], fallback to item['sections'])
    sections = None
    rag = item.get("rag")
    if isinstance(rag, dict):
        sections = rag.get("sections")
    if not isinstance(sections, list):
        sections = item.get("sections")

    # Build section_id -> traceable evidence[]
    sec_evidence = {}
    if isinstance(sections, list):
        for s in sections:
            if not isinstance(s, dict):
                continue
            sid = str(s.get("id") or "").strip()
            if not sid:
                continue
            evs = _drop_untraceable_evidence(s.get("evidence") or [])
            if evs:
                sec_evidence[sid] = evs

    risks = item.get("aiRisks") or []
    if not isinstance(risks, list):
        return

    for r in risks:
        if not isinstance(r, dict):
            continue

        rid = str(r.get("id") or "").strip()

        # Default: enforce non-floating evidence
        cur = r.get("evidence") or []
        cur = _drop_untraceable_evidence(cur)

        if rid.startswith("rag-section:"):
            # Format: rag-section:<reviewId>:<sectionId>
            parts = rid.split(":")
            if len(parts) >= 3:
                section_id = parts[-1].strip()
                evs = sec_evidence.get(section_id)
                if evs:
                    cur = evs

        r["evidence"] = cur

def _as_list(x):
    return x if isinstance(x, list) else []


def _as_dict(x):
    return x if isinstance(x, dict) else {}


@router.get("")
async def list_reviews():
    meta = DynamoMeta()
    items = meta.list_reviews() or []
    for it in items:
        _ensure_id_contract(it)

        # Contract normalization for UI compatibility:
        # - UI may still read `name` instead of `title`
        # - List view should rely on doc_count, not docs[]
        title = (it.get("title") or it.get("name") or "").strip()
        if not title:
            title = "Untitled"
        it["title"] = title
        it["name"] = title  # compat alias

        try:
            it["doc_count"] = int(it.get("doc_count") or it.get("docCount") or 0)
        except Exception:
            it["doc_count"] = 0

    return items



def _normalize_aiRisks_tiers_confidence(item: Dict[str, Any]) -> None:
    # Read-time normalization for stored aiRisks so existing reviews gain tier/confidence fields.
    # Deterministic-only enrichment. Does NOT call LLM.
    risks = item.get("aiRisks") or []
    if not isinstance(risks, list) or not risks:
        return

    sev_order = ["Informational", "Low", "Medium", "High", "Critical"]

    def sev_downshift(sev: str, steps: int) -> str:
        s = str(sev or "").strip() or "Informational"
        try:
            i = sev_order.index(s)
        except ValueError:
            return s
        return sev_order[max(0, i - int(steps or 0))]

    def conf_label(c: float) -> str:
        try:
            cf = float(c)
        except Exception:
            return "LOW"
        if cf >= 0.85:
            return "HIGH"
        if cf >= 0.65:
            return "MEDIUM"
        return "LOW"

    def tier_from_source(src: str, rid: str) -> str:
        s = str(src or "").strip()
        if s == "autoFlag":
            return "TIER3_FLAG"
        if s == "sectionDerived" or str(rid or "").startswith("rag-section:"):
            return "TIER2_SECTION"
        if s == "heuristic":
            return "TIER2_HEURISTIC"
        if s == "ai_only":
            return "TIER1_INFERENCE"
        return "TIER1_INFERENCE"

    def default_conf_for_tier(tier: str) -> float:
        t = str(tier or "").strip()
        if t == "TIER3_FLAG":
            return 0.90
        if t.startswith("TIER2_"):
            return 0.75
        return 0.50


    def _ev_to_bullets(evs, max_bullets: int = 4, max_len: int = 260) -> list[str]:
        if not isinstance(evs, list) or not evs:
            return []
        bullets = []
        seen = set()
        for ev in evs:
            if not isinstance(ev, dict):
                continue
            txt = str(ev.get("text") or "").replace("\n", " ").strip()
            if not txt:
                continue
            excerpt = txt[:max_len].strip()
            k = excerpt.lower()
            if k in seen:
                continue
            seen.add(k)
            bullets.append(excerpt)
            if len(bullets) >= int(max_bullets):
                break
        return bullets

    for r in risks:
        if not isinstance(r, dict):
            continue
        rid = str(r.get("id") or "").strip()
        src = str(r.get("source") or r.get("source_type") or r.get("sourceType") or "").strip()
        if src:
            r.setdefault("source", src)
            r.setdefault("source_type", src)

        tier = str(r.get("tier") or "").strip() or tier_from_source(src, rid)
        r["tier"] = tier

        c = r.get("confidence")
        try:
            cf = float(c) if c is not None else 0.0
        except Exception:
            cf = 0.0
        if cf <= 0.0:
            cf = default_conf_for_tier(tier)
        r["confidence"] = float(cf)
        r["confidence_label"] = str(r.get("confidence_label") or "").strip() or conf_label(cf)

        sev = str(r.get("severity") or "Informational").strip() or "Informational"
        if tier == "TIER1_INFERENCE" and sev in ("High", "Critical"):
            sev = "Medium"
        if tier.startswith("TIER2_") and sev == "Critical":
            sev = "High"
        if cf < 0.45:
            sev = sev_downshift(sev, 2)
        elif cf < 0.65:
            sev = sev_downshift(sev, 1)
        r["severity"] = sev

        # If this is a section risk with evidence but placeholder text, synthesize findings now
        try:
            if str(r.get("category") or "") == "RAG_SECTION":
                evs2 = r.get("evidence") or []
                desc2 = str(r.get("description") or "")
                rat2  = str(r.get("rationale") or "")
                if isinstance(evs2, list) and len(evs2) > 0 and ("No findings returned" in desc2 or "No findings returned" in rat2):
                    bullets = _ev_to_bullets(evs2, max_bullets=4, max_len=260)
                    if bullets:
                        r["findings"] = bullets
                        r["description"] = "Findings:\n- " + "\n- ".join(bullets)
                        r["rationale"] = r.get("description")
        except Exception:
            pass
    # Deterministic read-time fix: if section has evidence but placeholder text, derive Findings bullets from evidence.\r


@router.get("/{review_id}")
async def get_review(review_id: str, storage: StorageDep):
    """
    Get full review detail.

    Primary: Dynamo (META + embedded docs if present)
    Fallback: StorageProvider stores/reviews.json (legacy/local compatibility)
    """
    meta = DynamoMeta()
    item = meta.get_review_detail(review_id)

    # Fallback: storage-backed reviews.json
    if not item:
        try:
            raw = storage.get_object("stores/reviews.json")
            import json
            arr = json.loads(raw.decode("utf-8")) if isinstance(raw, (bytes, bytearray)) else json.loads(raw)
            if isinstance(arr, list):
                for r in arr:
                    if isinstance(r, dict) and (r.get("id") == review_id or r.get("review_id") == review_id):
                        item = r
                        break
        except Exception:
            item = None

    if not item:
        raise HTTPException(status_code=404, detail="Review not found")

    _backfill_aiRisks_evidence(item)

    _backfill_aiRisks_from_sections(item)
    _normalize_aiRisks_tiers_confidence(item)
    return _ensure_id_contract(item)

@router.post("")
async def upsert_review(review: Dict[str, Any], storage: StorageDep):
    """
    Create/update a review META row.

    IMPORTANT API CONTRACT:
    - Frontend expects response JSON to include `id`.
    - Client may POST without id (create) -> backend generates id.
    - Dynamo meta storage uses `review_id` internally; we return both.
    """
    if not isinstance(review, dict):
        raise HTTPException(status_code=400, detail="Invalid review payload (expected JSON object).")

    # Accept either id or review_id, otherwise generate one (create flow)
    review_id = (review.get("id") or review.get("review_id") or "").strip()
    if not review_id:
        review_id = str(uuid4())

    # Normalize incoming payload so downstream code can rely on both
    review["id"] = review_id
    review["review_id"] = review_id

    # recompute deterministic flags from docs in payload (safe even if docs absent)
    review = _attach_auto_flags_to_review(review)

    pdf_key = (review.get("pdf_key") or review.get("pdfKey") or "").strip() or None

    # NOTE: DynamoMeta currently persists META + pointers (pdf_key/extract pointers etc)
    meta = DynamoMeta()
    # Persist review meta fields + doc_count
    out = meta.upsert_review_meta(review_id, review=review, pdf_key=pdf_key)

    # Persist docs as child items (DOC#...)
    docs = review.get("docs") or []
    if isinstance(docs, list) and docs:
        meta.upsert_review_docs(review_id, docs)
    # Ensure response includes id (UI expects it) and normalize review_id
    if isinstance(out, dict):
        out["review_id"] = review_id
        out["id"] = review_id
    else:
        out = {"review_id": review_id, "id": review_id}

    # Return detail payload so UI can immediately show title + docs
    detail = meta.get_review_detail(review_id) or out
    return _ensure_id_contract(detail)
@router.delete("/{review_id}")
async def delete_review(review_id: str):
    # Minimal delete: delete META row only (for mock). Later we can delete DOC*/RAGRUN* rows too.
    meta = DynamoMeta()
    pk = f"REVIEW#{review_id}"
    meta.table.delete_item(Key={"pk": pk, "sk": "META"})
    return {"ok": True}










