# backend/reviews/router.py
from __future__ import annotations

import json
import os
from typing import List, Dict, Any
from fastapi import APIRouter, HTTPException, Request
from schemas import AnalyzeRequestModel, AnalyzeResponseModel
from core.llm_client import call_llm_for_review
from flags.service import scan_text_for_flags
from core.config import REVIEWS_FILE
from fastapi import Depends
from auth.jwt import get_current_user


from core.deps import StorageDep
router = APIRouter(
    prefix="/reviews",
    tags=["reviews"],
    dependencies=[Depends(get_current_user)],
)


# ---------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------

def _read_reviews_file(storage) -> List[Dict[str, Any]]:
    """Load reviews.json as a list of dicts.

    Preferred: StorageProvider key "stores/reviews.json"
    Fallback: legacy filesystem REVIEWS_FILE
    """
    key = "stores/reviews.json"

    # 1) StorageProvider (preferred)
    try:
    # storage injected by caller (StorageDep)
        raw = storage.get_object(key).decode("utf-8", errors="ignore")
        data = json.loads(raw) if raw.strip() else []
        return data if isinstance(data, list) else []
    except Exception:
        pass

    # 2) Legacy filesystem fallback
    if not os.path.exists(REVIEWS_FILE):
        return []
    try:
        with open(REVIEWS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_reviews_file(reviews: List[Dict[str, Any]], storage) -> None:
    """Persist reviews.json via StorageProvider.

    Storage key: stores/reviews.json
    """
    key = "stores/reviews.json"
    # storage injected by caller (StorageDep)
    try:
        payload = json.dumps(reviews, indent=2, ensure_ascii=False).encode("utf-8", errors="ignore")
        storage.put_object(key=key, data=payload, content_type="application/json", metadata=None)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to persist reviews store: {exc}")

# Auto-flags helpers (hit_key + snippets)
# ---------------------------------------------------------------------


def _build_hit_key(doc_id: str, flag_id: str, line: int, index: int) -> str:
    """
    Stable identifier for a specific hit within a review.

    Example: "doc-1:dfars_7012:182:0"
    """
    return f"{doc_id}:{flag_id}:{line}:{index}"


def _attach_auto_flags_to_review(review: Dict[str, Any]) -> Dict[str, Any]:
    """
    Compute auto flags for each document in the review and populate:

      review["autoFlags"] = {
          "hits": [...],
          "summary": {...},
          "hitsByDoc": { docId: [...] },
          "explainReady": bool
      }

    Each per-doc hit is enriched with:
      - docId
      - docName
      - snippet  (short excerpt from the matched text)
      - hit_key  (stable ID used by /flags/explain)
    """
    docs = review.get("docs") or []
    per_doc_hits: List[dict] = []
    hits_by_doc: Dict[str, List[dict]] = {}

    # --------------------------------------------
    # 1) Per-doc scanning (no usage tracking)
    # --------------------------------------------
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

        # Enrich each hit with docId, docName, snippet, hit_key
        for idx, h in enumerate(scan_result.get("hits") or []):
            flag_id = h.get("id") or h.get("flagId") or h.get("label") or "flag"
            line = h.get("line") or 0

            # Keep your existing approach: short snippet based on matched text
            # (we also have a more sophisticated fallback in /flags/explain)
            match_text = (h.get("match") or "")[:240]

            enriched = dict(h)
            enriched["docId"] = doc_id
            enriched["docName"] = doc_name
            enriched["snippet"] = match_text
            enriched["hit_key"] = _build_hit_key(doc_id, flag_id, line, idx)

            doc_hits.append(enriched)
            per_doc_hits.append(enriched)

        hits_by_doc[doc_id] = doc_hits

    # --------------------------------------------
    # 2) Combined scan (WITH usage tracking)
    # --------------------------------------------
    full_text = "\n".join(full_text_parts).strip()
    if full_text:
        combined = scan_text_for_flags(full_text, record_usage=True)
        summary = combined.get("summary")
    else:
        summary = None

    explain_ready = bool(summary)

    review["autoFlags"] = {
        "hits": per_doc_hits,
        "summary": summary,
        "hitsByDoc": hits_by_doc,
        "explainReady": explain_ready,
    }

    return review


# ---------------------------------------------------------------------
# GET /reviews
# ---------------------------------------------------------------------


@router.get("")
async def list_reviews(storage: StorageDep):
    """Return the full list of saved reviews."""
    return _read_reviews_file(storage)
@router.get("/{review_id}")
async def get_review(review_id: str, storage: StorageDep):
    """Return a single review by id."""
    reviews = _read_reviews_file(storage)
    review = next((r for r in (reviews or []) if str(r.get("id")) == str(review_id)), None)
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")
    return review

@router.post("")
async def upsert_review(review: Dict[str, Any], storage: StorageDep):
    """
    Upsert a review AND auto-generate backend flags.

    autoFlags structure:
    {
      "hits": [...],
      "summary": {...},
      "hitsByDoc": { docId: [...] },
      "explainReady": bool
    }
    """
    if "id" not in review:
        raise HTTPException(
            status_code=400,
            detail="Review must include 'id'.",
        )

    # Recompute autoFlags from the current docs (including hit_key)
    review = _attach_auto_flags_to_review(review)

    # Upsert into reviews.json
    reviews = _read_reviews_file(storage)
    idx = next(
        (i for i, r in enumerate(reviews) if r.get("id") == review["id"]),
        None,
    )

    if idx is None:
        reviews.append(review)
    else:
        reviews[idx] = review

    _write_reviews_file(reviews, storage)
    return review


# ---------------------------------------------------------------------
# POST /reviews/analyze - AI contract analysis WITH knowledge context
# ---------------------------------------------------------------------


@router.post("/analyze", response_model=AnalyzeResponseModel)
async def analyze_review(req: AnalyzeRequestModel):
    """
    Run LLM analysis for a single contract document,
    with optional knowledge_context via knowledge_doc_ids.
    """
    try:
        summary = await call_llm_for_review(req)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"LLM error: {exc}",
        )

    # risks are currently handled in doc-level intel in AiPanel-based calls
    return AnalyzeResponseModel(
        summary=summary,
        risks=[],
        doc_type=None,
        deliverables=[],
    )


# ---------------------------------------------------------------------
# DELETE /reviews/{id}
# ---------------------------------------------------------------------


@router.delete("/{review_id}")
async def delete_review(review_id: str, storage: StorageDep):
    reviews = _read_reviews_file(storage)
    new_list = [r for r in reviews if r.get("id") != review_id]
    if len(new_list) == len(reviews):
        raise HTTPException(status_code=404, detail="Review not found")
    _write_reviews_file(new_list, storage)
    return {"ok": True}

