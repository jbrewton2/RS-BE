from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

from core.settings import get_settings, Settings

from providers.storage import StorageProvider
from providers.vectorstore import VectorStore
from providers.jobs import JobRunner
from providers.llm import LLMProvider

from providers.impl.storage_local_files import LocalFilesStorageProvider
from providers.impl.storage_s3 import S3StorageProvider

from providers.impl.vector_opensearch import OpenSearchVectorStore  # No more pgvector or disabled vector

try:
    from providers.impl.llm_bedrock import BedrockLLMProvider  # type: ignore
except Exception:
    BedrockLLMProvider = None  # type: ignore

from providers.impl.jobs_local_inline import LocalInlineJobRunner


@dataclass(frozen=True)
class Providers:
    settings: Settings
    storage: StorageProvider
    vector: VectorStore
    jobs: JobRunner
    llm: Optional[LLMProvider]  # optional path; most routes still use core.llm_client


def _get_attr(obj, name: str, default=None):
    try:
        return getattr(obj, name)
    except Exception:
        return default


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    try:
        v = os.environ.get(name, default)
    except Exception:
        v = default
    if v is None:
        return default
    v = str(v).strip()
    return v if v else default


def _build_storage(settings: Settings) -> StorageProvider:
    storage_cfg = _get_attr(settings, "storage", None)
    provider = (_get_attr(storage_cfg, "provider", None) or _get_attr(storage_cfg, "mode", None) or "local").strip().lower()

    # Env override wins (runtime truth)
    env_provider = (os.environ.get("STORAGE_MODE") or os.environ.get("STORAGE_PROVIDER") or "").strip().lower()
    if env_provider:
        provider = env_provider

    if provider == "local":
        return LocalFilesStorageProvider()

    if provider == "s3":
        return S3StorageProvider.from_env()

    raise RuntimeError(f"Unsupported storage provider: {provider}")


def _build_vector(settings: Settings) -> VectorStore:
    """
    Vector store factory.

    Production (GovCloud css-mock):
      VECTOR_STORE=opensearch -> OpenSearchVectorStore (SigV4/IRSA)

    Pytest / unit tests:
      MUST NOT instantiate OpenSearch.
      If pytest hits this factory (e.g. importing main.py), return a safe Noop vector store.
    """
    def _is_pytest() -> bool:
        try:
            import sys
            if "pytest" in sys.modules:
                return True
        except Exception:
            pass
        return (os.getenv("PYTEST_CURRENT_TEST") is not None) or (os.getenv("CSS_TESTING") == "1")

    class _NoopVectorStore:
        def query(self, *args, **kwargs):
            return []
        def upsert_chunks(self, *args, **kwargs):
            return None
        def delete_review(self, *args, **kwargs):
            return None

    # Under pytest, never touch OpenSearch unless explicitly enabled.
    if _is_pytest() and os.getenv("CSS_TEST_OPENSEARCH") != "1":
        return _NoopVectorStore()

    # Prefer env override; fall back to settings if present
    mode = (os.environ.get("VECTOR_STORE") or os.environ.get("VECTOR_PROVIDER") or "").strip().lower()
    if not mode:
        vec_cfg = _get_attr(settings, "vector", None)
        mode = str(_get_attr(vec_cfg, "provider", "") or "").strip().lower()

    if mode == "opensearch":
        return OpenSearchVectorStore()

    raise RuntimeError(
        f"VECTOR_STORE must be 'opensearch' in production. Got: {mode!r}. "
        "In tests, inject FakeVector or rely on NoopVectorStore."
    )


def _build_jobs(settings: Settings) -> JobRunner:
    return LocalInlineJobRunner()


def _build_llm(settings: Settings) -> Optional[LLMProvider]:
    """
    Bedrock-only policy (GovCloud runtime truth).

    Pytest safety:
      - Unit tests MUST NOT require AWS_* env vars or IRSA.
      - If pytest hits provider init (e.g., importing main.py), return None unless explicitly enabled.
    """
    import os

    def _is_pytest() -> bool:
        try:
            import sys
            if "pytest" in sys.modules:
                return True
        except Exception:
            pass
        return (os.getenv("PYTEST_CURRENT_TEST") is not None) or (os.getenv("CSS_TESTING") == "1")

    # Under pytest, never touch Bedrock unless explicitly enabled.
    if _is_pytest() and os.getenv("CSS_TEST_BEDROCK") != "1":
        return None

    provider = (getattr(settings.llm, "provider", "") or "").strip().lower()
    env_provider = (os.environ.get("LLM_PROVIDER") or "").strip().lower()
    if env_provider:
        provider = env_provider

    if provider == "bedrock" and BedrockLLMProvider is not None:
        return BedrockLLMProvider.from_env()

    # Bedrock-only: no other providers allowed
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








