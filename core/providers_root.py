# backend/core/providers_root.py
from __future__ import annotations

from backend.providers.factory import get_providers


def init_providers():
    """
    Composition root for dependency providers.

    Phase 0.5: returns the provider container but does not change any behavior
    because nothing uses these providers yet.
    """
    return get_providers()
