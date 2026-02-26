# rag/router.py
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from auth.jwt import get_current_user
from core.providers import providers_from_request
from rag.contracts import RagAnalyzeRequest, RagAnalyzeResponse
from rag.service import rag_analyze_review, _owner_for_section  # noqa: F401

logger = logging.getLogger(__name__)

# main.py includes routers with prefix="/api"
# so this must be "/rag" (not "/api/rag") to yield "/api/rag/*"
router = APIRouter(prefix="/rag", tags=["rag"], dependencies=[Depends(get_current_user)])

# Guardrail: prevent the /api/api regression
assert not router.prefix.startswith("/api"), "Router prefix must not start with /api (main.py adds /api)."


def _ensure_section_owners(payload: Any) -> Any:
    """
    Final API-boundary guardrail:
    - If sections exist, ensure each section has a non-empty 'owner'
    - Handles dict sections OR Pydantic model sections
    """
    try:
        if isinstance(payload, dict):
            sections = payload.get("sections")
        else:
            sections = getattr(payload, "sections", None)

        if not sections:
            return payload

        for sec in sections:
            if isinstance(sec, dict):
                sid_val = sec.get("id")
                owner_val = sec.get("owner")
            else:
                sid_val = getattr(sec, "id", None)
                owner_val = getattr(sec, "owner", None)

            sid = (sid_val or "").strip().lower()
            owner = (owner_val or "").strip()

            if not owner:
                owner = _owner_for_section(sid)
                if isinstance(sec, dict):
                    sec["owner"] = owner
                else:
                    try:
                        setattr(sec, "owner", owner)
                    except Exception:
                        pass

        return payload
    except Exception:
        # Never break the endpoint due to owner enrichment
        return payload


@router.post(
    "/analyze",
    response_model=RagAnalyzeResponse,
    response_model_exclude_none=True,
)
def analyze(req: RagAnalyzeRequest, providers=Depends(providers_from_request)):
    try:
        auth = (request.headers.get("authorization") or request.headers.get("Authorization") or "").strip()
        token = ""
        if auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()

        result = rag_analyze_review(
            storage=providers.storage,
            vector=providers.vector,
            llm=providers.llm,
            review_id=req.review_id,
            mode=req.mode,
            analysis_intent=req.analysis_intent,
            heuristic_hits=req.heuristic_hits,
            context_profile=req.context_profile,
            top_k=req.top_k,
            force_reingest=req.force_reingest,
            debug=req.debug,
        )

        if result is None:
            logger.error(
                "rag_analyze_review returned None (review_id=%s mode=%s intent=%s profile=%s top_k=%s reingest=%s)",
                req.review_id,
                req.mode,
                req.analysis_intent,
                req.context_profile,
                req.top_k,
                req.force_reingest,
            )
            raise HTTPException(status_code=500, detail="RAG analyze failed: service returned no result")

        result = _ensure_section_owners(result)

        # Normalize to dict for response model validation
        if not isinstance(result, dict):
            try:
                result = result.model_dump()
            except Exception:
                result = dict(result)

        # Boundary defaults
        result.setdefault("review_id", req.review_id)
        result.setdefault("mode", req.mode)
        result.setdefault("top_k", req.top_k)
        result.setdefault("analysis_intent", req.analysis_intent)
        result.setdefault("context_profile", req.context_profile)

        # summary required
        summary_val = result.get("summary")
        if summary_val is None:
            result["summary"] = ""
        elif not isinstance(summary_val, str):
            result["summary"] = str(summary_val)

        return RagAnalyzeResponse.model_validate(result)

    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except HTTPException:
        # Do not wrap FastAPI errors
        raise
    except Exception as e:
        logger.exception("RAG analyze failed")
        raise HTTPException(status_code=500, detail=f"RAG analyze failed: {type(e).__name__}") from e

