from __future__ import annotations

from typing import Any, Dict, List, Optional

from providers.llm import LLMProvider


class OllamaLLMProvider(LLMProvider):
    """
    Local provider wrapper.

    Phase 0.75:
    - embed_texts is not wired yet (we don't have embeddings plumbing in current backend)
    - generate is intentionally NOT used yet by routes (no behavior change)
    """

    def embed_texts(self, texts: List[str], model: Optional[str] = None) -> List[List[float]]:
        raise NotImplementedError("Embeddings not wired yet (Phase 0.75).")

    def generate(
        self,
        prompt: str,
        model: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        # Not used yet by any route; placeholder for later.
        return {
            "text": "",
            "metadata": {"provider": "ollama", "model": model, "params": params or {}},
        }

