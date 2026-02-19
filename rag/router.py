# rag/router.py
from __future__ import annotations

import inspect
import os
import traceback
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException

from core.dynamo_meta import DynamoMeta
from core.providers import providers_from_request
from rag.contracts import RagAnalyzeRequest, RagAnalyzeResponse
from rag.service import (
    _owner_for_section,  # noqa: F401
    ingest_review_docs,
    rag_analyze_review,
)

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
            # read id + owner (dict OR model)
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
        # Never fail the endpoint because of a guardrail; main response validation still applies.
        return payload


def _env_bool(name: str, default: bool) -> bool:
    v = str(os.getenv(name, "1" if default else "0")).strip().lower()
    return v in ("1", "true", "yes", "y", "on")


def _safe_decode(b: bytes) -> str:
    try:
        return b.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _get_review_extract_text(storage, review_id: str) -> Optional[str]:
    """
    Load extracted text for review_id via Dynamo META -> StorageProvider.
    We expect Dynamo keys like:
      extract_text_s3_key = "extract/<rid>/raw_text.txt"
    StorageProvider will map to real S3 key (in your case it writes under stores/*).
    """
    meta = DynamoMeta()
    item = meta.get_review_meta(review_id)
    if not item:
        return None

    # DynamoMeta stores snake_case fields; be liberal if older items exist.
    key = (
        item.get("extract_text_s3_key")
        or item.get("extract_text_key")
        or item.get("extractTextS3Key")
        or item.get("extractTextKey")
    )
    if not key or not isinstance(key, str):
        return None

    key = key.strip()
    if not key:
        return None

    try:
        raw = storage.get_object(key)
    except Exception:
        return None

    if not raw:
        return None

    text = _safe_decode(raw).strip()
    return text or None


def _call_ingest_review_docs(*, storage, vector, llm, review_id: str, docs: List[Dict[str, Any]], force: bool) -> None:
    """
    Call ingest_review_docs without guessing its exact parameter list.
    We map by parameter names using inspect.signature and pass only what exists.
    """
    sig = inspect.signature(ingest_review_docs)
    kwargs: Dict[str, Any] = {}

    for name in sig.parameters.keys():
        n = name.lower()

        if n in ("storage",):
            kwargs[name] = storage
        elif n in ("vector", "vectorstore"):
            kwargs[name] = vector
        elif n in ("llm", "llmprovider", "embedder"):
            kwargs[name] = llm
        elif n in ("review_id", "reviewid", "document_id", "documentid"):
            kwargs[name] = review_id
        elif n in ("docs", "review_docs", "documents", "reviewdocuments"):
            kwargs[name] = docs
        elif n in ("force_reingest", "force", "reingest"):
            kwargs[name] = force

    # If ingest_review_docs takes positional-only args (unlikely), this will throw;
    # but your function is defined with named args in rag/service.py in practice.
    ingest_review_docs(**kwargs)


@router.post(
    "/analyze",
    response_model=RagAnalyzeResponse,
    response_model_exclude_none=True,
)
def analyze(req: RagAnalyzeRequest, providers=Depends(providers_from_request)):
    """
    Router-level backstop: ensure ingestion exists for OpenSearch-based vector store
    by pulling extracted text from Dynamo+S3 and indexing it before analyze.

    This fixes "Insufficient evidence retrieved" when DOC_ID_COUNT=0 in OpenSearch.
    """
    try:
        # If VECTOR_STORE is opensearch, we must ensure review_id has chunks indexed.
        # Default: ingest-on-analyze enabled (can be disabled via env).
        ingest_on_analyze = _env_bool("RAG_INGEST_ON_ANALYZE", True)

        # Only attempt this if we actually have an llm (needed for embeddings) and storage.
        # If llm is None, ingestion would fail; analysis may still run in a degraded mode.
        if ingest_on_analyze and providers.llm is not None:
            review_id = (req.review_id or "").strip()
            if review_id:
                text = _get_review_extract_text(providers.storage, review_id)
                if text:
                    docs = [
                        {
                            "id": review_id,
                            "name": f"review:{review_id}",
                            # rag/service.py reads content/text; give both to be safe
                            "content": text,
                            "text": text,
                        }
                    ]

                    # Only ingest when requested OR when force_reingest is true OR when explicitly enabled.
                    # (We ingest even when not forced because your current OpenSearch index may be empty.)
                    _call_ingest_review_docs(
                        storage=providers.storage,
                        vector=providers.vector,
                        llm=providers.llm,
                        review_id=review_id,
                        docs=docs,
                        force=bool(req.force_reingest),
                    )

        # Now run analysis (uses whatever vector store is active)
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

        # API boundary backstop: owner must always be present for sections
        result = _ensure_section_owners(result)

        # Ensure the response is validated/coerced into the declared contract
        return RagAnalyzeResponse.model_validate(result)

    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        # Preserve stack traces in logs; give a stable message to callers.
        if str(os.getenv("RAG_TRACEBACK", "0")).strip() == "1":
            print("[RAG][TRACEBACK] " + traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"RAG analyze failed: {type(e).__name__}") from e
