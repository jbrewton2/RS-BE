from __future__ import annotations

import json
import os
from enum import Enum
from typing import Dict, Optional

from pydantic import BaseModel

from core.config import ORG_POSTURE_SUMMARY

LLM_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "llm_config.json")


class LLMProvider(str, Enum):
    LOCAL_OLLAMA = "local_ollama"
    REMOTE_HTTP = "remote_http"


class LLMConfig(BaseModel):
    provider: LLMProvider = LLMProvider.LOCAL_OLLAMA

    local_model: str = os.getenv("OLLAMA_MODEL", "llama3.1")
    local_api_url: str = os.getenv("OLLAMA_API_URL", "http://localhost:11434/api/chat")

    remote_base_url: Optional[str] = None
    remote_path: str = "/v1/chat/completions"
    remote_model: Optional[str] = None
    remote_api_key: Optional[str] = None
    remote_extra_headers: Dict[str, str] = {}

    org_posture: Optional[str] = None
    prompt_override: Optional[str] = None

    effective_org_posture: Optional[str] = None


def _compute_effective_org_posture(org_posture: Optional[str]) -> str:
    if org_posture:
        text = org_posture.strip()
        if text:
            return text
    return ORG_POSTURE_SUMMARY


def load_llm_config() -> LLMConfig:
    if not os.path.exists(LLM_CONFIG_PATH):
        cfg = LLMConfig()
        cfg.effective_org_posture = _compute_effective_org_posture(cfg.org_posture)
        save_llm_config(cfg)
        return cfg

    with open(LLM_CONFIG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    cfg = LLMConfig(**data)
    cfg.effective_org_posture = _compute_effective_org_posture(cfg.org_posture)
    return cfg


def save_llm_config(cfg: LLMConfig) -> None:
    data = cfg.model_dump()
    data.pop("effective_org_posture", None)

    with open(LLM_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
