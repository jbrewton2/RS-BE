# backend/reviews/router.py
from __future__ import annotations

from typing import List, Dict, Any
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


@router.get("")
async def list_reviews():
    meta = DynamoMeta()
    return meta.list_reviews()


@router.get("/{review_id}")
async def get_review(review_id: str):
    meta = DynamoMeta()
    item = meta.get_review_meta(review_id)
    if not item:
        raise HTTPException(status_code=404, detail="Review not found")
    return item


@router.post("")
async def upsert_review(review: Dict[str, Any], storage: StorageDep):
    if "id" not in review:
        raise HTTPException(status_code=400, detail="Review must include 'id'.")

    # recompute deterministic flags from docs in payload
    review = _attach_auto_flags_to_review(review)

    review_id = str(review["id"])
    pdf_key = (review.get("pdf_key") or review.get("pdfKey") or "").strip() or None

    # NOTE: we are not hashing PDF bytes here (that is set by extract-by-key or upload pipeline)
    meta = DynamoMeta()
    out = meta.upsert_review_meta(review_id, pdf_key=pdf_key)

    # store the full review payload as an artifact in S3 (optional) - for now keep it in Dynamo meta only
    # If you want full payload persisted, we can add a pointer+hash in Dynamo to an S3 JSON.

    return out


@router.delete("/{review_id}")
async def delete_review(review_id: str):
    # Minimal delete: delete META row only (for mock). Later we can delete DOC*/RAGRUN* rows too.
    meta = DynamoMeta()
    pk = f"REVIEW#{review_id}"
    meta.table.delete_item(Key={"pk": pk, "sk": "META"})
    return {"ok": True}