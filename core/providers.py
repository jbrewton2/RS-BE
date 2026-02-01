from __future__ import annotations

from fastapi import Request

def providers_from_request(request: Request):
    """
    Canonical provider accessor for ALL routers.

    Routers should never call globals like legacy provider accessors or legacy provider accessors.
    Providers are attached once during app startup as request.app.state.providers.
    """
    try:
        return request.app.state.providers
    except Exception as exc:
        raise RuntimeError("Providers not initialized on app.state (startup/lifespan not executed).") from exc
from fastapi import FastAPI
from providers.factory import get_providers

def init_providers(app: FastAPI):
    """
    Canonical provider initialization.
    Called once during app startup/lifespan. Attaches Providers onto app.state.
    """
    app.state.providers = get_providers()
    return app.state.providers
