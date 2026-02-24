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


@router.get("/{review_id}")
async def get_review(review_id: str):
    meta = DynamoMeta()
    item = meta.get_review_detail(review_id)
    if not item:
        raise HTTPException(status_code=404, detail="Review not found")
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




