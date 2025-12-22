from __future__ import annotations

from typing import Protocol, runtime_checkable, Optional, Dict, Any, List


@runtime_checkable
class LLMProvider(Protocol):
    """
    LLM/Embeddings abstraction.

    Phase 0: interface only (not wired).
    """

    def embed_texts(self, texts: List[str], model: Optional[str] = None) -> List[List[float]]: ...

    def generate(
        self,
        prompt: str,
        model: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Return shape is intentionally flexible Phase 0.
        Later we will enforce a strict schema including citations + metadata.
        """
        ...
