# backend/pricing/llm_pricing_store.py
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parent.parent
PRICING_FILE = BASE_DIR / "llm_pricing.json"


class ModelPricing(BaseModel):
    model: str
    input_per_1k: float
    output_per_1k: float


class LlmPricingConfig(BaseModel):
    # Global defaults when a specific model isn't configured
    default_input_per_1k: float = 0.0
    default_output_per_1k: float = 0.0
    # Use Field(default_factory=...) to avoid mutable default
    models: List[ModelPricing] = Field(default_factory=list)


def load_llm_pricing() -> LlmPricingConfig:
    """
    Load pricing config from llm_pricing.json.
    If file does not exist or is invalid, return a safe default config.
    """
    if not PRICING_FILE.exists():
        return LlmPricingConfig(
            default_input_per_1k=0.0,
            default_output_per_1k=0.0,
            models=[],
        )

    try:
        with PRICING_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        # Corrupt file or read error -> safe default
        return LlmPricingConfig(
            default_input_per_1k=0.0,
            default_output_per_1k=0.0,
            models=[],
        )

    # Pydantic will validate/normalize the structure
    try:
        return LlmPricingConfig(**data)
    except Exception:
        # If structure is wrong, also fall back
        return LlmPricingConfig(
            default_input_per_1k=0.0,
            default_output_per_1k=0.0,
            models=[],
        )


def save_llm_pricing(cfg: LlmPricingConfig) -> None:
    """
    Persist pricing config to llm_pricing.json (pretty-printed).
    """
    PRICING_FILE.parent.mkdir(parents=True, exist_ok=True)
    with PRICING_FILE.open("w", encoding="utf-8") as f:
        json.dump(cfg.dict(), f, indent=2, sort_keys=True)


def get_model_pricing(model: str, cfg: Optional[LlmPricingConfig] = None) -> ModelPricing:
    """
    Resolve the pricing row for a given model.
    Falls back to default_input_per_1k/default_output_per_1k.
    """
    if cfg is None:
        cfg = load_llm_pricing()

    # Try to find an explicit model override
    for m in cfg.models:
        if m.model == model:
            return m

    # No explicit model -> synthesize a ModelPricing from defaults
    return ModelPricing(
        model=model or "default",
        input_per_1k=cfg.default_input_per_1k,
        output_per_1k=cfg.default_output_per_1k,
    )


def compute_cost_usd(
    model: str,
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    cfg: Optional[LlmPricingConfig] = None,
) -> float:
    """
    Compute USD cost for a single LLM call given token counts and pricing config.

    - input_tokens / output_tokens are token counts (not thousands).
    - default_* fields are per 1K tokens.
    """
    if cfg is None:
        cfg = load_llm_pricing()

    pricing = get_model_pricing(model, cfg)

    in_tokens = max(int(input_tokens or 0), 0)
    out_tokens = max(int(output_tokens or 0), 0)

    in_cost = (in_tokens / 1000.0) * pricing.input_per_1k
    out_cost = (out_tokens / 1000.0) * pricing.output_per_1k

    return float(in_cost + out_cost)
