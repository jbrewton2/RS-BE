from __future__ import annotations

from fastapi import APIRouter, HTTPException, Depends  # ✅ auth

# ✅ AUTH
from auth.jwt import get_current_user

# ✅ use relative import so it works when backend is a package
from .llm_pricing_store import (
    LlmPricingConfig,
    load_llm_pricing,
    save_llm_pricing,
)

router = APIRouter(
    prefix="/llm-pricing",
    tags=["llm-pricing"],
    dependencies=[Depends(get_current_user)],  # ✅ protect all /llm-pricing
)


@router.get("", response_model=LlmPricingConfig)
def get_llm_pricing() -> LlmPricingConfig:
    """
    Return the current LLM pricing configuration.
    """
    try:
        return load_llm_pricing()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to load pricing: {exc}") from exc


@router.put("", response_model=LlmPricingConfig)
def update_llm_pricing(cfg: LlmPricingConfig) -> LlmPricingConfig:
    """
    Update and persist the LLM pricing configuration.
    """
    try:
        save_llm_pricing(cfg)
        return cfg
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save pricing: {exc}") from exc

