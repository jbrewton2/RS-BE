from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class ProviderSettings:
    """
    Phase 0 contract: settings exist, but nothing in the app must depend on them yet.

    Defaults preserve current local behavior when/if wired later.
    """
    storage_provider: str = "local"      # local | azureblob (future)
    llm_provider: str = "ollama"         # ollama | azureopenai | mock (future)
    job_runner: str = "local"            # local | batch (future)
    vector_store: str = "disabled"       # disabled | pgvector (future)


def load_provider_settings() -> ProviderSettings:
    # Intentionally permissive defaults so Phase 0 is zero-impact.
    return ProviderSettings(
        storage_provider=os.getenv("CSS_STORAGE_PROVIDER", "local"),
        llm_provider=os.getenv("CSS_LLM_PROVIDER", "ollama"),
        job_runner=os.getenv("CSS_JOB_RUNNER", "local"),
        vector_store=os.getenv("CSS_VECTOR_STORE", "disabled"),
    )
