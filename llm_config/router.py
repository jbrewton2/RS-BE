# backend/llm_config/router.py
from __future__ import annotations

from fastapi import APIRouter, HTTPException
import httpx

from backend.llm_config_store import (
    LLMConfig,
    LLMProvider,
    load_llm_config,
    save_llm_config,
)

router = APIRouter(
    prefix="/llm-config",
    tags=["llm-config"],
)


@router.get("", response_model=LLMConfig)
async def get_llm_config_route():
    """
    Return the current LLM configuration.
    """
    try:
        return load_llm_config()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to load LLM config: {exc}",
        )


@router.put("", response_model=LLMConfig)
async def update_llm_config_route(cfg: LLMConfig):
    """
    Update and persist the LLM configuration.

    Validation:
      - remote_http requires remote_base_url + remote_model
    """
    if cfg.provider == LLMProvider.REMOTE_HTTP:
        if not cfg.remote_base_url:
            raise HTTPException(
                status_code=400,
                detail="remote_base_url is required when provider=remote_http",
            )
        if not cfg.remote_model:
            raise HTTPException(
                status_code=400,
                detail="remote_model is required when provider=remote_http",
            )

    try:
        save_llm_config(cfg)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to save LLM config: {exc}",
        )

    return cfg


@router.post("/test-remote")
async def test_remote_llm_route():
    """
    Test the configured remote LLM endpoint by sending a minimal
    chat completion-style request.

    Uses:
      - remote_base_url + remote_path
      - remote_model
      - remote_api_key (Bearer) if present
    """
    cfg = load_llm_config()

    if cfg.provider != LLMProvider.REMOTE_HTTP:
        raise HTTPException(
            status_code=400,
            detail="LLM provider is not remote_http. Enable the remote endpoint first.",
        )

    if not cfg.remote_base_url or not cfg.remote_model:
        raise HTTPException(
            status_code=400,
            detail="remote_base_url and remote_model are required to test remote endpoint.",
        )

    base = cfg.remote_base_url.rstrip("/")
    path = (cfg.remote_path or "/v1/chat/completions").lstrip("/")
    url = f"{base}/{path}"

    # Minimal OpenAI-style chat-completions payload
    payload = {
        "model": cfg.remote_model,
        "messages": [
            {"role": "system", "content": "Health check for remote LLM endpoint."},
            {"role": "user", "content": "Reply with 'OK'."},
        ],
        "max_tokens": 5,
    }

    headers: dict[str, str] = {}
    if cfg.remote_api_key:
        headers["Authorization"] = f"Bearer {cfg.remote_api_key}"
    extra_headers = cfg.remote_extra_headers or {}
    headers.update(extra_headers)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Remote LLM test request failed: {exc}",
        )

    return {"ok": True, "message": "Remote LLM endpoint responded successfully."}
