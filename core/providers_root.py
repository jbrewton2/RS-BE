from __future__ import annotations

from providers.factory import get_providers


def init_providers():
    """
    Composition root for dependency providers.
    """
    return get_providers()
