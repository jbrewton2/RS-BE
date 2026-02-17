from __future__ import annotations
import os

from dataclasses import dataclass
import os
from functools import lru_cache
import os
from typing import Optional
import os

from core.settings import get_settings, Settings
import os

from providers.storage import StorageProvider
import os
from providers.vectorstore import VectorStore
import os
from providers.jobs import JobRunner
import os
from providers.llm import LLMProvider
import os

from providers.impl.storage_local_files import LocalFilesStorageProvider
import os
from providers.impl.vector_disabled import DisabledVectorStore
import os
from providers.impl.jobs_local_inline import LocalInlineJobRunner
import os

# Optional impls
try:
    from providers.impl.storage_minio import MinioStorageProvider  # type: ignore
except Exception:  # pragma: no cover
    MinioStorageProvider = None  # type: ignore

try:
    from providers.impl.llm_ollama import OllamaLLMProvider  # type: ignore
except Exception:  # pragma: no cover
    OllamaLLMProvider = None  # type: ignore


try:  # pragma: no cover
    from providers.impl.llm_bedrock import BedrockLLMProvider  # type: ignore
except Exception:  # pragma: no cover
    BedrockLLMProvider = None  # type: ignore

# Ã¢Å“â€¦ pgvector impl (this MUST exist if VECTOR_STORE=pgvector)
try:
    from providers.impl.vector_pgvector import PgVectorStore  # type: ignore
except Exception:  # pragma: no cover
    PgVectorStore = None  # type: ignore


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
        import os

        v = os.environ.get(name, default)
    except Exception:
        v = default
    if v is None:
        return default
    v = str(v).strip()
    return v if v else default


def _build_storage(settings: Settings) -> StorageProvider:
    """
    Storage selection is SETTINGS/ENV driven.

    Supported:
      - local
      - minio (S3-compatible)
    """
    storage_cfg = _get_attr(settings, "storage", None)
    provider = _get_attr(storage_cfg, "provider", None) or _get_attr(storage_cfg, "mode", None) or "local"
    provider = str(provider).strip().lower()

    # Normalize aliases
    if provider in ("files", "local_files", "localfiles"):
        provider = "local"
    if provider in ("objectstore", "object_store", "s3", "blob"):
        provider = "minio"

    if provider == "local":
        return LocalFilesStorageProvider()

    if provider == "minio":
        if MinioStorageProvider is None:
            raise RuntimeError(
                "settings.storage.provider=minio but providers.impl.storage_minio.MinioStorageProvider "
                "could not be imported."
            )

        endpoint = (
            _get_attr(storage_cfg, "minio_endpoint", None)
            or _get_attr(storage_cfg, "endpoint", None)
            or _env("MINIO_ENDPOINT")
            or _env("S3_ENDPOINT")
        )
        bucket = (
            _get_attr(storage_cfg, "minio_bucket", None)
            or _get_attr(storage_cfg, "bucket", None)
            or _env("MINIO_BUCKET")
            or _env("S3_BUCKET")
        )
        access_key = (
            _get_attr(storage_cfg, "minio_access_key", None)
            or _get_attr(storage_cfg, "access_key", None)
            or _env("MINIO_ACCESS_KEY")
            or _env("S3_ACCESS_KEY")
        )
        secret_key = (
            _get_attr(storage_cfg, "minio_secret_key", None)
            or _get_attr(storage_cfg, "secret_key", None)
            or _env("MINIO_SECRET_KEY")
            or _env("S3_SECRET_KEY")
        )

        missing = [k for k, v in {
            "MINIO_ENDPOINT": endpoint,
            "MINIO_BUCKET": bucket,
            "MINIO_ACCESS_KEY": access_key,
            "MINIO_SECRET_KEY": secret_key,
        }.items() if not v]

        if missing:
            raise RuntimeError("MinIO storage selected but missing config: " + ", ".join(missing))

        return MinioStorageProvider(
            endpoint=endpoint,
            bucket=bucket,
            access_key=access_key,
            secret_key=secret_key,
        )

    raise RuntimeError(f"Unsupported storage provider: {provider}")


def _build_vector(settings: Settings) -> VectorStore:
    """
    Vector selection is SETTINGS/ENV driven.

    - VECTOR_STORE=pgvector  -> PgVectorStore (local dev)
    - otherwise              -> DisabledVectorStore
    """
    import os

    provider = ""
    try:
        provider = (getattr(getattr(settings, "vector", None), "provider", "") or "").strip().lower()
    except Exception:
        provider = ""

    env_provider = (os.environ.get("VECTOR_STORE") or os.environ.get("VECTOR_PROVIDER") or "").strip().lower()
    if env_provider:
        provider = env_provider

    if provider == "pgvector":
        if PgVectorStore is None:
            raise RuntimeError("VECTOR_STORE=pgvector but PgVectorStore could not be imported.")
        return PgVectorStore()

    return DisabledVectorStore()



def _build_jobs(settings: Settings) -> JobRunner:
    return LocalInlineJobRunner()


def _build_llm(settings: Settings) -> Optional[LLMProvider]:
    """
    LLM provider factory (authoritative).
    Honors settings first, then env overrides for local/dev parity.
    """
    provider = (getattr(settings.llm, "provider", "") or "").strip().lower()

    # Env overrides (keep compatibility with older naming)
    env_provider = (os.environ.get("LLM_PROVIDER") or os.environ.get("OLLAMA_PROVIDER") or "").strip().lower()
    if env_provider:
        provider = env_provider

    if provider == "bedrock" and BedrockLLMProvider is not None:
        return BedrockLLMProvider.from_env()

    if provider == "ollama" and OllamaLLMProvider is not None:
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


