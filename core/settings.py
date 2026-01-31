from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import List


def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return default if v is None else str(v)


def _env_float(name: str, default: float) -> float:
    raw = _env(name, "")
    if not raw.strip():
        return default
    try:
        return float(raw)
    except Exception:
        return default


def _env_int(name: str, default: int) -> int:
    raw = _env(name, "")
    if not raw.strip():
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _env_csv_floats(name: str, default: List[float]) -> List[float]:
    raw = _env(name, "")
    if not raw.strip():
        return default
    out: List[float] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(float(part))
        except Exception:
            continue
    return out or default


@dataclass(frozen=True)
class LLMSettings:
    # provider is an implementation selector (ollama today, bedrock tomorrow, etc.)
    provider: str
    api_url: str
    model: str
    timeout_seconds: float
    connect_timeout_seconds: float
    max_attempts: int
    backoff_seconds: List[float]


@dataclass(frozen=True)
class StorageSettings:
    # provider is an implementation selector (local today, object_store tomorrow)
    provider: str


@dataclass(frozen=True)
class AuthSettings:
    provider: str


@dataclass(frozen=True)
class Settings:
    llm: LLMSettings
    storage: StorageSettings
    auth: AuthSettings


def _load_llm_settings() -> LLMSettings:
    # New canonical env names (provider-agnostic)
    provider = (_env("LLM_PROVIDER", "") or _env("OLLAMA_PROVIDER", "") or "ollama").strip().lower()

    # Canonical env keys
    api_url = (_env("LLM_API_URL", "") or "").strip()
    model = (_env("LLM_MODEL", "") or "").strip()

    timeout_seconds = _env_float("LLM_TIMEOUT_SECONDS", 240.0)
    connect_timeout_seconds = _env_float("LLM_CONNECT_TIMEOUT_SECONDS", 10.0)
    max_attempts = _env_int("LLM_MAX_ATTEMPTS", 2)
    backoff_seconds = _env_csv_floats("LLM_BACKOFF_SECONDS", [0.3])

    # 1-release compatibility mapping from OLLAMA_* if LLM_* not set
    if not api_url:
        api_url = (_env("OLLAMA_API_URL", "") or "http://localhost:11434/api/generate").strip()
    if not model:
        model = (_env("OLLAMA_MODEL", "") or "llama3.1").strip()

    if "LLM_TIMEOUT_SECONDS" not in os.environ and "OLLAMA_TIMEOUT_SECONDS" in os.environ:
        timeout_seconds = _env_float("OLLAMA_TIMEOUT_SECONDS", timeout_seconds)
    if "LLM_MAX_ATTEMPTS" not in os.environ and "OLLAMA_MAX_ATTEMPTS" in os.environ:
        max_attempts = _env_int("OLLAMA_MAX_ATTEMPTS", max_attempts)
    if "LLM_BACKOFF_SECONDS" not in os.environ and "OLLAMA_BACKOFF_SECONDS" in os.environ:
        backoff_seconds = _env_csv_floats("OLLAMA_BACKOFF_SECONDS", backoff_seconds)
    if "LLM_CONNECT_TIMEOUT_SECONDS" not in os.environ and "OLLAMA_CONNECT_TIMEOUT_SECONDS" in os.environ:
        connect_timeout_seconds = _env_float("OLLAMA_CONNECT_TIMEOUT_SECONDS", connect_timeout_seconds)

    # clamp/sanity
    timeout_seconds = max(5.0, float(timeout_seconds))
    connect_timeout_seconds = max(1.0, float(connect_timeout_seconds))
    max_attempts = max(1, min(int(max_attempts), 5))
    backoff_seconds = [max(0.0, float(x)) for x in (backoff_seconds or [0.3])] or [0.3]

    return LLMSettings(
        provider=provider,
        api_url=api_url,
        model=model,
        timeout_seconds=timeout_seconds,
        connect_timeout_seconds=connect_timeout_seconds,
        max_attempts=max_attempts,
        backoff_seconds=backoff_seconds,
    )


def _load_storage_settings() -> StorageSettings:
    provider = (_env("STORAGE_PROVIDER", "") or "local").strip().lower()
    return StorageSettings(provider=provider)


def _load_auth_settings() -> AuthSettings:
    provider = (_env("AUTH_PROVIDER", "") or "keycloak").strip().lower()
    return AuthSettings(provider=provider)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        llm=_load_llm_settings(),
        storage=_load_storage_settings(),
        auth=_load_auth_settings(),
    )