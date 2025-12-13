# backend/knowledge/models.py
from __future__ import annotations

from typing import List, Optional
from pydantic import BaseModel


class KnowledgeDocMeta(BaseModel):
    """
    Metadata about a single knowledge document.

    These correspond to entries in knowledge_store.json and files in
    KNOWLEDGE_DOCS_DIR (usually as .txt).
    """
    id: str
    title: str
    filename: str
    doc_type: Optional[str] = None  # e.g., "SSP", "Questionnaire", "Policy"
    tags: List[str] = []
    created_at: str  # ISO timestamp
    size_bytes: int


class KnowledgeDocListResponse(BaseModel):
    """
    Response wrapper for listing all knowledge docs.
    """
    docs: List[KnowledgeDocMeta]
