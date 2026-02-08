# rag/router.py
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
import os
import traceback

from core.providers import providers_from_request
from rag.contracts import RagAnalyzeRequest, RagAnalyzeResponse
from rag.service import rag_analyze_review

# main.py includes routers with prefix="/api"
# so this must be "/rag" (not "/api/rag") to yield "/api/rag/*"
router = APIRouter(prefix="/rag", tags=["rag"])

# Guardrail: prevent the /api/api regression
assert not router.prefix.startswith("/api"), "Router prefix must not start with /api (main.py adds /api)."


@router.post(
    "/analyze",
    response_model=RagAnalyzeResponse,
    response_model_exclude_none=True,
)
def analyze(req: RagAnalyzeRequest, providers=Depends(providers_from_request)):
    try:
        return rag_analyze_review(
            storage=providers.storage,
            vector=providers.vector,
            llm=providers.llm,
            review_id=req.review_id,
            mode=req.mode,
            analysis_intent=req.analysis_intent,
            context_profile=req.context_profile,
            top_k=req.top_k,
            force_reingest=req.force_reingest,
            debug=req.debug,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        # Preserve stack traces in logs; give a stable message to callers.
        if str(os.getenv("RAG_TRACEBACK","0")).strip() == "1":
            print("[RAG][TRACEBACK] " + traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"RAG analyze failed: {type(e).__name__}") from e


