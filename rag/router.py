from __future__ import annotations

import os
import traceback
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth.jwt import get_current_user
from core.deps import get_storage, get_vector, get_llm
from rag.service import ingest_review_docs, query_review, rag_analyze_review

router = APIRouter(
    prefix="/rag",
    tags=["rag"],
    dependencies=[Depends(get_current_user)],
)


def _trace_enabled() -> bool:
    return (os.getenv("RAG_TRACEBACK", "") or "").strip().lower() in {"1", "true", "yes", "on"}


class IngestStatusRequest(BaseModel):
    review_id: str = Field(..., description="Review id in reviews.json")
    chunk_size: int = 1500
    overlap: int = 250


class DebugRequest(BaseModel):
    review_id: str
    question: str
    top_k: int = 12


class AnalyzeRequest(BaseModel):
    review_id: str
    top_k: int = 12
    force_reingest: bool = False

    # NEW: mode controls question set + prompt behavior
    # "review_summary" is the bounded workflow for Reviews.
    # "chat" reserved for future if chat migrates to /rag/analyze.
    mode: Optional[str] = None


@router.get("/ingest-status")
async def ingest_status(storage=Depends(get_storage)):
    """
    Optional quick status: lists reviews + docs count from StorageProvider only.
    (No vector DB dependency.)
    """
    try:
        from reviews.router import _read_reviews_file

        reviews = _read_reviews_file(storage)
        out = []
        for r in reviews:
            docs = r.get("docs") or []
            out.append(
                {
                    "reviewId": r.get("id"),
                    "name": r.get("name"),
                    "docCount": len(docs) if isinstance(docs, list) else 0,
                }
            )
        return {"ok": True, "reviews": out}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ingest-status failed: {e}")


@router.post("/ingest-status")
async def ingest_status_post(req: IngestStatusRequest, storage=Depends(get_storage), vector=Depends(get_vector), llm=Depends(get_llm)):
    """
    Trigger ingest for a review by reading its docs from StorageProvider.
    """
    try:
        return ingest_review_docs(
            storage=storage,
            vector=vector,
            llm=llm,
            review_id=req.review_id,
            chunk_size=req.chunk_size,
            overlap=req.overlap,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Review not found")
    except Exception as e:
        if _trace_enabled():
            traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"ingest failed: {e}")


@router.post("/debug")
async def rag_debug(req: DebugRequest, vector=Depends(get_vector), llm=Depends(get_llm)):
    """
    Return top_k hits + scores for visibility.
    """
    try:
        hits = query_review(
            vector=vector,
            llm=llm,
            question=req.question,
            top_k=req.top_k,
            filters={"review_id": req.review_id},
        )
        return {"ok": True, "hits": hits}
    except Exception as e:
        if _trace_enabled():
            traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"debug failed: {e}")


@router.post("/analyze")
async def rag_analyze(req: AnalyzeRequest, storage=Depends(get_storage), vector=Depends(get_vector), llm=Depends(get_llm)):
    """
    True RAG analysis:
      - (optional) ingest docs
      - retrieve evidence
      - synthesize summary
    """
    try:
        return rag_analyze_review(
            storage=storage,
            vector=vector,
            llm=llm,
            review_id=req.review_id,
            top_k=req.top_k,
            force_reingest=req.force_reingest,
            mode=req.mode,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Review not found")
    except Exception as e:
        if _trace_enabled():
            traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"rag analyze failed: {e}")
