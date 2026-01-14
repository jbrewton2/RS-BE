# NOTE:
# This module is a composition root. Avoid importing it from core/* at import-time.
# Use lazy imports (inside functions) or core/providers_root.py init_providers().


from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .settings import ProviderSettings, load_provider_settings
from .storage import StorageProvider
from .llm import LLMProvider
from .vectorstore import VectorStore
from .jobs import JobRunner

from providers.impl.llm_ollama import OllamaLLMProvider
from providers.impl.storage_local_files import LocalFilesStorageProvider
from providers.impl.vector_disabled import DisabledVectorStore
from providers.impl.jobs_local_inline import LocalInlineJobRunner


@dataclass(frozen=True)
class Providers:
    settings: ProviderSettings
    storage: StorageProvider
    llm: LLMProvider
    vector: VectorStore
    jobs: JobRunner


_cached: Optional[Providers] = None


def get_providers() -> Providers:
    global _cached
    if _cached is None:
        settings = load_provider_settings()
        _cached = Providers(
            settings=settings,
            storage=LocalFilesStorageProvider(),
            llm=OllamaLLMProvider(),
            vector=DisabledVectorStore(),
            jobs=LocalInlineJobRunner(),
        )
    return _cached
