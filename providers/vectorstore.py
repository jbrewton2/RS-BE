from __future__ import annotations

from typing import Protocol, runtime_checkable, Optional, Dict, Any, List


@runtime_checkable
class VectorStore(Protocol):
    """
    Vector index abstraction.

    Phase 0: interface only (not wired).
    """

    def upsert_chunks(self, document_id: str, chunks: List[Dict[str, Any]]) -> None: ...

    def upsert_embeddings(self, embeddings: List[Dict[str, Any]]) -> None: ...

    def query(
        self,
        query_embedding: List[float],
        top_k: int = 10,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]: ...

    def delete_by_document(self, document_id: str) -> None: ...
