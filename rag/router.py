# rag/router.py
from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks

from auth.jwt import get_current_user
from core.providers import providers_from_request
from rag.contracts import RagAnalyzeRequest, RagAnalyzeResponse, RagAnalyzeJobResponse, RagAnalyzeJobStatusResponse
from rag.service import rag_analyze_review, _owner_for_section  # noqa: F401
from rag.jobs_store import RagJobStore

logger = logging.getLogger(__name__)

_JOB_STORE = None

def _job_store():
    global _JOB_STORE
    if _JOB_STORE is None:
        _JOB_STORE = RagJobStore()
    return _JOB_STORE




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


def _strip_debug_fields(d: dict) -> dict:
    """
    DynamoDB item max is 400KB. Strip large debug fields before persisting results.
    Always remove debug_context, retrieval_debug and any debug_* keys.
    """
    try:
        if not isinstance(d, dict):
            return d
        # explicit known-large keys
        for k in [
            "debug_context",
            "retrieval_debug",
            "debug_prompt_prefix",
            "debug_llm_raw_prefix",
            "debug_llm_text_preview",
            "debug_llm_smoke_prompt_2000_preview",
            "debug_llm_smoke_prompt_6000_preview",
        ]:
            d.pop(k, None)
        # remove any debug_* keys
        for k in list(d.keys()):
            if isinstance(k, str) and k.startswith("debug_"):
                d.pop(k, None)
        return d
    except Exception:
        return d
@router.post("/analyze_async", response_model=RagAnalyzeJobResponse)
def analyze_async(req: RagAnalyzeRequest, request: Request, background: BackgroundTasks, providers=Depends(providers_from_request)):
    """
    Async wrapper to avoid ALB ~60s timeouts.
    Request contract uses RagAnalyzeRequest.
    """
    auth = (request.headers.get("authorization") or request.headers.get("Authorization") or "").strip()
    token = ""
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()

    job_id = str(uuid.uuid4())

    # Persist queued job immediately
    _job_store().create(
        job_id=job_id,
        review_id=str(req.review_id),
        request_obj=req.model_dump(),
        ttl_seconds=86400,
    )

    def _run() -> None:
        try:
            _job_store().update(job_id, status="running", progress_pct=5, message="running")
            result = rag_analyze_review(
                storage=providers.storage,
                vector=providers.vector,
                llm=providers.llm,
                review_id=req.review_id,
                token=token,
                mode=req.mode,
                analysis_intent=req.analysis_intent,
                heuristic_hits=req.heuristic_hits,
                context_profile=req.context_profile,
                top_k=req.top_k,
                force_reingest=req.force_reingest,
                debug=req.debug,
            )

            _job_store().update(job_id, progress_pct=40, message="analysis complete")

            # keep same boundary behavior as /analyze
            result = _ensure_section_owners(result)

            if not isinstance(result, dict):
                try:
                    result = result.model_dump()
                except Exception:
                    result = dict(result)

            result.setdefault("review_id", req.review_id)
            result.setdefault("mode", req.mode)
            result.setdefault("top_k", req.top_k)
            result.setdefault("analysis_intent", req.analysis_intent)
            result.setdefault("context_profile", req.context_profile)

            summary_val = result.get("summary")
            if summary_val is None:
                result["summary"] = ""
            elif not isinstance(summary_val, str):
                result["summary"] = str(summary_val)

            _job_store().update(job_id, progress_pct=90, message="persisting result")
            result = _strip_debug_fields(result)
            _job_store().put_result(job_id, result)

        except Exception as e:
            logger.exception("RAG analyze_async failed (job_id=%s)", job_id)
            _job_store().update(job_id, status="failed", progress_pct=100, message="failed", error=f"{type(e).__name__}: {e}")

    background.add_task(_run)
    return {"job_id": job_id, "status": "queued"}


@router.get("/analyze_status", response_model=RagAnalyzeJobStatusResponse)
def analyze_status(job_id: str):
    """
    Returns job status and progress for async analyze jobs.
    """
    try:
        item = _job_store().get(job_id)
        return {
            "job_id": item.get("job_id"),
            "review_id": item.get("review_id"),
            "status": item.get("status"),
            "progress_pct": item.get("progress_pct"),
            "message": item.get("message"),
            "error": item.get("error"),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
        }
    except KeyError:
        raise HTTPException(status_code=404, detail="job_id not found")


@router.get("/analyze_result", response_model=RagAnalyzeResponse, response_model_exclude_none=True)
def analyze_result(job_id: str):
    """
    Returns final RagAnalyzeResponse once the job succeeds.
    """
    try:
        item = _job_store().get(job_id)
        status = item.get("status")
        if status != "succeeded":
            raise HTTPException(status_code=409, detail=f"job not complete (status={status})")
        raw = item.get("result")
        if not raw:
            raise HTTPException(status_code=500, detail="job succeeded but result missing")

        # stored as JSON string
        if isinstance(raw, str):
            import json
            raw_obj = json.loads(raw)
        else:
            raw_obj = raw

        return RagAnalyzeResponse.model_validate(raw_obj)
    except KeyError:
        raise HTTPException(status_code=404, detail="job_id not found")

