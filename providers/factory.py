from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

from core.settings import get_settings, Settings

from providers.storage import StorageProvider
from providers.vectorstore import VectorStore
from providers.jobs import JobRunner
from providers.llm import LLMProvider

# Existing impls (DO NOT rename classes unless you change the impl files too)
from providers.impl.storage_local_files import LocalFilesStorageProvider
from providers.impl.vector_disabled import DisabledVectorStore
from providers.impl.jobs_local_inline import LocalInlineJobRunner

# Optional: you may or may not want to use the providers.impl.llm_ollama provider yet
try:
    from providers.impl.llm_ollama import OllamaLLMProvider
except Exception:  # pragma: no cover
    OllamaLLMProvider = None  # type: ignore


@dataclass(frozen=True)
class Providers:
    settings: Settings
    storage: StorageProvider
    vector: VectorStore
    jobs: JobRunner
    llm: Optional[LLMProvider]  # core.llm_client still drives LLM calls today


def _build_storage(settings: Settings) -> StorageProvider:
    if settings.storage.provider == "local":
        return LocalFilesStorageProvider()
    # future: "object_store" -> S3/Blob/MinIO adapter behind one interface
    raise RuntimeError(f"Unsupported storage provider: {settings.storage.provider}")


def _build_vector(settings: Settings) -> VectorStore:
    # today: disabled unless you add a vector implementation
    return DisabledVectorStore()


def _build_jobs(settings: Settings) -> JobRunner:
    # today: inline placeholder
    return LocalInlineJobRunner()


def _build_llm(settings: Settings) -> Optional[LLMProvider]:
    # Leave None for now (core.llm_client is the active path)
    # If you want to start using a provider object, enable this:
    if settings.llm.provider == "ollama" and OllamaLLMProvider is not None:
        return OllamaLLMProvider()
    return None


@lru_cache(maxsize=1)
def get_providers() -> Providers:
    s = get_settings()
    return Providers(
        settings=s,
        storage=_build_storage(s),
        vector=_build_vector(s),
        jobs=_build_jobs(s),
        llm=_build_llm(s),
    )