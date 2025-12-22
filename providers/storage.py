from __future__ import annotations

from typing import Protocol, runtime_checkable, Optional, Dict, Any


@runtime_checkable
class StorageProvider(Protocol):
    """
    Object storage abstraction.

    Phase 0: interface only (not wired).
    """

    def put_object(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: Optional[Dict[str, str]] = None,
    ) -> None: ...

    def get_object(self, key: str) -> bytes: ...

    def head_object(self, key: str) -> Dict[str, Any]: ...

    def delete_object(self, key: str) -> None: ...

    def presign_url(self, key: str, ttl_seconds: int = 900) -> str: ...
