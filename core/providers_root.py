from __future__ import annotations

def _providers():
    from providers.factory import get_providers
    return get_providers()




def init_providers():
    """
    Composition root for dependency providers.
    """
    return _providers()

