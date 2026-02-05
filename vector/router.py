from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional

from core.deps import get_providers


router = APIRouter(prefix="/vector", tags=["vector"])


class IngestRequest(BaseModel):
    document_id: str
    doc_name: Optional[str] = None
    text: str
    chunk_size: int = 1500
    overlap: int = 250


class QueryRequest(BaseModel):
    question: str
    top_k: int = 10
    filters: Optional[Dict[str, Any]] = None


@router.post("/ingest")
def ingest(req: IngestRequest):
    from vector.service import ingest_document

    providers = get_providers()
    if not providers or not providers.vector:
        raise HTTPException(status_code=500, detail="Vector provider not available")

    try:
        return ingest_document(
            vector=providers.vector,
            document_id=req.document_id,
            doc_name=req.doc_name,
            text=req.text,
            chunk_size=req.chunk_size,
            overlap=req.overlap,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ingest failed: {e}")


@router.post("/query")
def query(req: QueryRequest):
    from vector.service import query as query_vec

    providers = get_providers()
    if not providers or not providers.vector:
        raise HTTPException(status_code=500, detail="Vector provider not available")

    try:
        hits = query_vec(
            vector=providers.vector,
            question=req.question,
            top_k=req.top_k,
            filters=req.filters,
        )
        return {"hits": hits}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Query failed: {e}")

