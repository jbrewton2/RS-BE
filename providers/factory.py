from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .settings import ProviderSettings, load_provider_settings
from .storage import StorageProvider
from .llm import LLMProvider
from .vectorstore import VectorStore
from .jobs import JobRunner
from backend.providers.impl.llm_ollama import OllamaLLMProvider
from backend.providers.impl.storage_local_files import LocalFilesStorageProvider
from backend.providers.impl.vector_disabled import DisabledVectorStore
from backend.providers.impl.jobs_local_inline import LocalInlineJobRunner


class _NotWiredError(RuntimeError):
    pass


@dataclass(frozen=True)
class Providers:
    """
    Central container for providers.

    Phase 0: This is not used by the app yet.
    """
    settings: ProviderSettings
    storage: StorageProvider
    llm: LLMProvider
    vector: VectorStore
    jobs: JobRunner


class _NullStorage(StorageProvider):
    def put_object(self, key: str, data: bytes, content_type: str = "application/octet-stream", metadata=None) -> None:
        raise _NotWiredError("StorageProvider not wired yet (Phase 0).")

    def get_object(self, key: str) -> bytes:
        raise _NotWiredError("StorageProvider not wired yet (Phase 0).")

    def head_object(self, key: str):
        raise _NotWiredError("StorageProvider not wired yet (Phase 0).")

    def delete_object(self, key: str) -> None:
        raise _NotWiredError("StorageProvider not wired yet (Phase 0).")

    def presign_url(self, key: str, ttl_seconds: int = 900) -> str:
        raise _NotWiredError("StorageProvider not wired yet (Phase 0).")


class _NullLLM(LLMProvider):
    def embed_texts(self, texts, model=None):
        raise _NotWiredError("LLMProvider not wired yet (Phase 0).")

    def generate(self, prompt: str, model=None, params=None):
        raise _NotWiredError("LLMProvider not wired yet (Phase 0).")


class _NullVector(VectorStore):
    def upsert_chunks(self, document_id: str, chunks):
        raise _NotWiredError("VectorStore not wired yet (Phase 0).")

    def upsert_embeddings(self, embeddings):
        raise _NotWiredError("VectorStore not wired yet (Phase 0).")

    def query(self, query_embedding, top_k: int = 10, filters=None):
        raise _NotWiredError("VectorStore not wired yet (Phase 0).")

    def delete_by_document(self, document_id: str) -> None:
        raise _NotWiredError("VectorStore not wired yet (Phase 0).")


class _NullJobs(JobRunner):
    def submit(self, job_type: str, payload):
        raise _NotWiredError("JobRunner not wired yet (Phase 0).")

    def status(self, job_id: str):
        raise _NotWiredError("JobRunner not wired yet (Phase 0).")

    def result(self, job_id: str):
        raise _NotWiredError("JobRunner not wired yet (Phase 0).")


_cached: Optional[Providers] = None


def get_providers() -> Providers:
    """
    Phase 0: returns null providers so nothing changes unless explicitly wired later.
    """
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


