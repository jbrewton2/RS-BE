from __future__ import annotations

from typing import Any, Dict, List, Optional
import os
import httpx

from providers.llm import LLMProvider


def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return default if v is None else str(v)


def _derive_ollama_embeddings_url(api_url: str) -> str:
    """
    Common:
      generate:   http://.../api/generate
      embeddings: http://.../api/embeddings
    """
    u = (api_url or "").strip()
    if not u:
        return "http://localhost:11434/api/embeddings"
    if u.endswith("/api/generate"):
        return u.replace("/api/generate", "/api/embeddings")
    if u.endswith("/api/chat"):
        return u.replace("/api/chat", "/api/embeddings")
    # If user already pointed to embeddings, keep it
    return u


class OllamaLLMProvider(LLMProvider):
    """
    Local provider wrapper (Ollama).

    We now implement embeddings so pgvector can work.
    """

    def embed_texts(self, texts: List[str], model: Optional[str] = None) -> List[List[float]]:
        if not texts:
            return []

        # Model: prefer explicit EMBED_MODEL, else fall back to LLM_MODEL, else a sane default
        embed_model = (
            (model or "").strip()
            # Preferred generic names
            or _env("EMBEDDING_MODEL", "").strip()
            or _env("EMBED_MODEL", "").strip()
            or _env("LLM_EMBED_MODEL", "").strip()
            # Fallbacks (kept for backwards-compat)
            or "nomic-embed-text"
        )

        # Endpoint: prefer explicit, else derive from LLM_API_URL / OLLAMA_API_URL
        base_generate_url = (_env("LLM_API_URL", "").strip() or _env("OLLAMA_API_URL", "").strip() or "http://localhost:11434/api/generate")
        embed_url = (_env("EMBED_API_URL", "").strip() or _derive_ollama_embeddings_url(base_generate_url))

        timeout = float(_env("EMBED_TIMEOUT_SECONDS", _env("LLM_TIMEOUT_SECONDS", "240") or "240") or "240")

        out: List[List[float]] = []

        # Call per text (Ollama embeddings endpoint is single-input in many builds)
        with httpx.Client(timeout=timeout) as client:
            for t in texts:
                payload = {"model": embed_model, "prompt": t}
                r = client.post(embed_url, json=payload)
                r.raise_for_status()
                data = r.json()
                vec = data.get("embedding") or data.get("data") or None
                if not isinstance(vec, list):
                    raise RuntimeError(f"Ollama embeddings response missing 'embedding' list. keys={list(data.keys())}")
                out.append([float(x) for x in vec])

        return out

    def generate(
        self,
        prompt: str,
        model: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        # Generation (chat by default; supports /api/chat or /api/generate)
        p = (params or {}).copy()

        # Endpoint selection (prefer generic LLM_API_URL; fall back to OLLAMA_API_URL; then default)
        api_url = (_env("LLM_API_URL", "").strip() or _env("OLLAMA_API_URL", "").strip() or "http://localhost:11434/api/chat").strip()

        # Model selection (prefer generic LLM_MODEL; fall back to OLLAMA_MODEL; then function arg)
        chosen_model = (
            (model or "").strip()
            or _env("OLLAMA_MODEL", "").strip()
            or _env("LLM_MODEL", "").strip()
            or "llama3.1"
        )

        timeout = float(_env("LLM_TIMEOUT_SECONDS", _env("OLLAMA_TIMEOUT_SECONDS", "240") or "240") or "240")

        # Temperature: params override, else env, else default
        temperature = float(p.get("temperature") or _env("LLM_TEMPERATURE", "0.2") or "0.2")
        # ULTRA FAST MODE (CPU-friendly hard cap)
        try:
            max_tokens = int(_env("LLM_MAX_TOKENS", "96") or "96")
        except Exception:
            max_tokens = 96

        # Hard ceiling
        if max_tokens > 96:
            max_tokens = 96
        if max_tokens < 16:
            max_tokens = 16
        if (_env("RAG_FAST", "") or "").strip().lower() in {"1","true","yes","on"}:
            cap = int((_env("RAG_FAST_MAX_TOKENS", "256") or "256") or "256")
            max_tokens = min(max_tokens, cap)
            
            # Hard ceiling (env-driven) to prevent runaway generations (CPU-friendly)
            try:
                hard_ceiling = int(_env("LLM_HARD_MAX_TOKENS", "256") or "256")
            except Exception:
                hard_ceiling = 256
            if hard_ceiling < 16:
                hard_ceiling = 16
            if max_tokens > hard_ceiling:
                max_tokens = hard_ceiling
            

        # If user points at /api/generate, use prompt format; otherwise use chat messages format
        is_generate = api_url.endswith("/api/generate")

        if is_generate:
            payload = {
                "model": chosen_model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": temperature, "num_predict": max_tokens},
            }
        else:
            payload = {
                "model": chosen_model,
                "messages": [
                    {"role": "system", "content": p.get("system") or "You are a contract and risk analyst. Return plain text only."},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                "options": {"temperature": temperature, "num_predict": max_tokens},
            }

        with httpx.Client(timeout=timeout) as client:
            print(f"[LLM] generate endpoint={api_url} model={chosen_model}")
            try:
                print(f"[LLM] num_predict={max_tokens}")
            except Exception:
                pass
            try:
                r = client.post(api_url, json=payload)
                r.raise_for_status()
                data = r.json()
            except httpx.HTTPStatusError as e:
                status = e.response.status_code if e.response is not None else None
                if status != 404:
                    raise
                alt_url = None
                if api_url.endswith("/api/generate"):
                    alt_url = api_url[:-len("/api/generate")] + "/api/chat"
                elif api_url.endswith("/api/chat"):
                    alt_url = api_url[:-len("/api/chat")] + "/api/generate"
                if not alt_url:
                    raise
                if alt_url.endswith("/api/generate"):
                    alt_payload = {
                        "model": chosen_model,
                        "prompt": prompt,
                        "stream": False,
                        "options": {"temperature": temperature, "num_predict": max_tokens},
                    }
                else:
                    alt_payload = {
                        "model": chosen_model,
                        "messages": [
                            {"role": "system", "content": p.get("system") or "You are a contract and risk analyst. Return plain text only."},
                            {"role": "user", "content": prompt},
                        ],
                        "stream": False,
                        "options": {"temperature": temperature, "num_predict": max_tokens},
                    }
                r2 = client.post(alt_url, json=alt_payload)
                r2.raise_for_status()
                data = r2.json()

        # Parse common Ollama shapes:
        # - chat: {"message":{"role":"assistant","content":"..."}}
        # - generate: {"response":"..."}
        text = ""
        if isinstance(data, dict):
            if isinstance(data.get("message"), dict):
                text = (data["message"].get("content") or "").strip()
            elif "response" in data:
                text = (data.get("response") or "").strip()

        return {
            "text": text,
            "metadata": {
                "provider": "ollama",
                "model": chosen_model,
                "endpoint": api_url,
                "params": {"temperature": temperature, **{k: v for k, v in p.items() if k != "system"}},
                "raw_keys": list(data.keys()) if isinstance(data, dict) else [],
            },
        }












