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

# Optional: LLM provider
try:
    from providers.impl.llm_ollama import OllamaLLMProvider
except Exception:  # pragma: no cover
    OllamaLLMProvider = None  # type: ignore

# Optional: MinIO storage provider (only if you have it in your repo)
try:
    from providers.impl.storage_minio import MinioStorageProvider  # type: ignore
except Exception:  # pragma: no cover
    MinioStorageProvider = None  # type: ignore


@dataclass(frozen=True)
class Providers:
    settings: Settings
    storage: StorageProvider
    vector: VectorStore
    jobs: JobRunner
    llm: Optional[LLMProvider]  # core.llm_client still drives LLM calls today


def _get_attr(obj, name: str, default=None):
    try:
        return getattr(obj, name)
    except Exception:
        return default


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = default
    try:
        v = __import__("os").environ.get(name, default)
    except Exception:
        v = default
    if v is None:
        return default
    v = str(v).strip()
    return v if v else default


def _build_storage(settings: Settings) -> StorageProvider:
    """
    Storage selection is SETTINGS-DRIVEN.
    This fixes the current behavior where env says STORAGE_MODE=minio but factory always returns LocalFilesStorageProvider.

    Expected settings shape (best effort):
      settings.storage.provider: "local" | "minio" | "object_store"
      settings.storage.minio_endpoint / bucket / access_key / secret_key (optional)
    """

    storage_cfg = _get_attr(settings, "storage", None)
    provider = _get_attr(storage_cfg, "provider", None) or _get_attr(storage_cfg, "mode", None) or "local"
    provider = str(provider).strip().lower()

    # Normalize aliases
    if provider in ("files", "local_files", "localfiles"):
        provider = "local"
    if provider in ("objectstore", "object_store", "s3", "blob"):
        # treat as minio-compatible object store in local dev
        provider = "minio"

    if provider == "local":
        # default behavior as before
        return LocalFilesStorageProvider()

    if provider == "minio":
        if MinioStorageProvider is None:
            raise RuntimeError(
                "settings.storage.provider=minio but providers.impl.storage_minio.MinioStorageProvider "
                "could not be imported. Ensure providers/impl/storage_minio.py exists and deps are installed."
            )

        # Pull from Settings if present, else env (supports your infra manifest/env)
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
            raise RuntimeError(
                "MinIO storage selected but required config is missing: "
                + ", ".join(missing)
                + ". Provide via settings.storage.* or env vars."
            )

        # Constructor signature is assumed to be endpoint/bucket/access_key/secret_key
        # (matches what we’ve been using in infra)
        return MinioStorageProvider(
            endpoint=endpoint,
            bucket=bucket,
            access_key=access_key,
            secret_key=secret_key,
        )

    raise RuntimeError(f"Unsupported storage provider: {provider}")


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
