from __future__ import annotations

import os
from typing import Any, Dict, Optional

from backend.core.config import FILES_DIR
from backend.providers.storage import StorageProvider


class LocalFilesStorageProvider(StorageProvider):
    """
    Local filesystem storage provider using backend.core.config.FILES_DIR.

    Phase 0.75:
    - Not yet used by /extract route (no behavior change)
    - Provides a consistent interface we can switch behind later
    """

    def _path(self, key: str) -> str:
        safe = key.replace("..", "").lstrip("/").replace("/", os.sep)
        return os.path.join(FILES_DIR, safe)

    def put_object(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: Optional[Dict[str, str]] = None,
    ) -> None:
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    def get_object(self, key: str) -> bytes:
        path = self._path(key)
        with open(path, "rb") as f:
            return f.read()

    def head_object(self, key: str) -> Dict[str, Any]:
        path = self._path(key)
        st = os.stat(path)
        return {"key": key, "size": st.st_size, "mtime": st.st_mtime}

    def delete_object(self, key: str) -> None:
        path = self._path(key)
        if os.path.exists(path):
            os.remove(path)

    def presign_url(self, key: str, ttl_seconds: int = 900) -> str:
        # Local dev: return a stable route path that backend already serves.
        # Caller is responsible for mapping key -> filename.
        return f"/files/{key}"
