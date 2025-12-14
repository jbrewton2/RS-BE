# backend/health/router.py
from fastapi import APIRouter
import os
import httpx

router = APIRouter(tags=["health"])

@router.get("/health")
def health():
    # Keep this super simple and always unauthenticated
    return {"ok": True}

@router.get("/health/llm")
async def health_llm():
    """
    Verifies:
      - Ollama is reachable
      - configured model exists in ollama tags
    """
    ollama_base = os.getenv("OLLAMA_BASE_URL")  # optional (if you want)
    ollama_api_url = os.getenv("OLLAMA_API_URL", "http://ollama:11434/api/generate")
    model = os.getenv("OLLAMA_MODEL", "llama3.1:8b-instruct-q4_K_M")

    # derive base URL from OLLAMA_API_URL if base not provided
    # e.g. http://ollama:11434/api/generate -> http://ollama:11434
    base = ollama_base or ollama_api_url.split("/api/")[0].rstrip("/")

    tags_url = f"{base}/api/tags"

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(tags_url)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        return {
            "ok": False,
            "llmReachable": False,
            "modelReady": False,
            "model": model,
            "error": str(e),
        }

    models = [m.get("name") for m in data.get("models", []) if isinstance(m, dict)]
    model_ready = model in models

    return {
        "ok": True,
        "llmReachable": True,
        "modelReady": model_ready,
        "model": model,
        "knownModels": models[:25],  # keep response bounded
    }
