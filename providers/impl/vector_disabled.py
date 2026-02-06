from __future__ import annotations

from typing import Any, Dict, List, Optional

from providers.vectorstore import VectorStore


class DisabledVectorStore(VectorStore):
    def upsert_chunks(self, document_id: str, chunks: List[Dict[str, Any]]) -> None:
        return

    def query(self, query_embedding: List[float], top_k: int = 10, filters: Optional[Dict[str, Any]] = None):
        return []

    def delete_by_document(self, document_id: str) -> None:
        return
