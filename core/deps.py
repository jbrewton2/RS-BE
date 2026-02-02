from __future__ import annotations

from typing import Annotated, Any
from fastapi import Depends, Request

from core.providers import providers_from_request


# -----------------------------
# Canonical provider access
# -----------------------------

def get_providers(request: Request) -> Any:
    """
    Canonical runtime provider resolver.

    Source of truth: request.app.state.providers
    """
    return providers_from_request(request)


ProvidersDep = Annotated[Any, Depends(get_providers)]


# -----------------------------
# Canonical service deps
# -----------------------------

def get_storage(request: Request) -> Any:
    """
    Canonical StorageProvider dependency.
    """
    return get_providers(request).storage


StorageDep = Annotated[Any, Depends(get_storage)]


def get_db(request: Request) -> Any:
    """
    Canonical DB provider dependency.
    EXPECTS: providers.db
    """
    return getattr(get_providers(request), "db")


DbDep = Annotated[Any, Depends(get_db)]


def get_vector(request: Request) -> Any:
    """
    Canonical VectorStore provider dependency.
    EXPECTS: providers.vector OR providers.vectorstore
    """
    p = get_providers(request)
    if hasattr(p, "vector"):
        return p.vector
    return getattr(p, "vectorstore")


VectorDep = Annotated[Any, Depends(get_vector)]


def get_llm(request: Request) -> Any:
    """
    Canonical LLM provider dependency.
    EXPECTS: providers.llm
    """
    return getattr(get_providers(request), "llm")


LLMDep = Annotated[Any, Depends(get_llm)]


def get_tasks(request: Request) -> Any:
    """
    Canonical Task/Queue provider dependency (if present).
    EXPECTS: providers.tasks OR providers.queue
    """
    p = get_providers(request)
    if hasattr(p, "tasks"):
        return p.tasks
    return getattr(p, "queue")


TasksDep = Annotated[Any, Depends(get_tasks)]