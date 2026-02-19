# rag/router.py
from __future__ import annotations

import os
import traceback
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from core.providers import providers_from_request
from rag.contracts import RagAnalyzeRequest, RagAnalyzeResponse
from rag.service import rag_analyze_review, _owner_for_section  # noqa: F401


# main.py includes routers with prefix="/api"
# so this must be "/rag" (not "/api/rag") to yield "/api/rag/*"
router = APIRouter(prefix="/rag", tags=["rag"])

# Guardrail: prevent the /api/api regression
assert not router.prefix.startswith("/api"), "Router prefix must not start with /api (main.py adds /api)."


def _ensure_section_owners(payload: Any) -> Any:
    """
    Final API-boundary guardrail:
    - If sections exist, ensure each section has a non-empty 'owner'
    - Handles dict sections OR Pydantic model sections
    """
    try:
        sections = None
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
        return payload


@router.post(
    "/analyze",
    response_model=RagAnalyzeResponse,
    response_model_exclude_none=True,
)
def analyze(req: RagAnalyzeRequest, providers=Depends(providers_from_request)):
    try:
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

        result = _ensure_section_owners(result)
        return RagAnalyzeResponse.model_validate(result)

    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        if str(os.getenv("RAG_TRACEBACK", "0")).strip() == "1":
            print("[RAG][TRACEBACK] " + traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"RAG analyze failed: {type(e).__name__}") from e