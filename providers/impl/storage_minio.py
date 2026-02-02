from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Dict, Any

from minio import Minio
from minio.error import S3Error

from providers.storage import StorageProvider


def _strip_http(endpoint: str) -> str:
    # Minio client expects "host:port" (no scheme)
    endpoint = (endpoint or "").strip()
    endpoint = endpoint.replace("http://", "").replace("https://", "")
    endpoint = endpoint.rstrip("/")
    return endpoint


@dataclass
class MinioStorageProvider(StorageProvider):
    """
    Minimal MinIO-backed implementation of StorageProvider.

    Env expected (your infra already sets these in the backend container):
      - MINIO_ENDPOINT (e.g. http://minio:9000)
      - MINIO_BUCKET   (e.g. css)
      - MINIO_ACCESS_KEY
      - MINIO_SECRET_KEY

    Notes:
      - We auto-create the bucket if missing.
      - Keys are treated as opaque strings (e.g. stores/question_bank.json).
      - This is synchronous and simple by design for the local stack.
    """

    endpoint: str
    bucket: str
    access_key: str
    secret_key: str
    secure: bool = False

    def __post_init__(self) -> None:
        host = _strip_http(self.endpoint)
        if not host:
            raise RuntimeError("MINIO_ENDPOINT is empty or invalid")

        self._client = Minio(
            host,
            access_key=self.access_key,
            secret_key=self.secret_key,
            secure=bool(self.secure),
        )

        # Ensure bucket exists
        try:
            if not self._client.bucket_exists(self.bucket):
                self._client.make_bucket(self.bucket)
        except Exception as e:
            raise RuntimeError(f"MinIO bucket init failed (bucket={self.bucket}): {e}") from e

    @classmethod
    def from_env(cls) -> "MinioStorageProvider":
        endpoint = os.getenv("MINIO_ENDPOINT", "http://minio:9000").strip()
        bucket = os.getenv("MINIO_BUCKET", "css").strip()
        access_key = os.getenv("MINIO_ACCESS_KEY", "").strip()
        secret_key = os.getenv("MINIO_SECRET_KEY", "").strip()

        if not access_key or not secret_key:
            raise RuntimeError("MINIO_ACCESS_KEY / MINIO_SECRET_KEY not set")

        # Derive secure from scheme (best-effort)
        secure = endpoint.lower().startswith("https://")
        return cls(
            endpoint=endpoint,
            bucket=bucket,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
        )

    def get_object(self, key: str) -> bytes:
        key = (key or "").lstrip("/")
        try:
            resp = self._client.get_object(self.bucket, key)
            try:
                return resp.read()
            finally:
                resp.close()
                resp.release_conn()
        except S3Error as e:
            # Not found should raise FileNotFoundError to match local provider behavior
            if getattr(e, "code", "") in ("NoSuchKey", "NoSuchObject"):
                raise FileNotFoundError(key) from e
            raise
        except Exception as e:
            # Normalize "missing" patterns
            msg = str(e)
            if "NoSuchKey" in msg or "NoSuchObject" in msg:
                raise FileNotFoundError(key) from e
            raise

    def put_object(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: Optional[Dict[str, str]] = None,
    ) -> None:
        key = (key or "").lstrip("/")
        if data is None:
            data = b""

        # MinIO put_object requires a stream and a length
        import io

        stream = io.BytesIO(data)
        length = len(data)

        # metadata headers must be strings
        meta: Dict[str, str] = {}
        if metadata:
            for k, v in metadata.items():
                if v is None:
                    continue
                meta[str(k)] = str(v)

        self._client.put_object(
            bucket_name=self.bucket,
            object_name=key,
            data=stream,
            length=length,
            content_type=content_type or "application/octet-stream",
            metadata=meta or None,
        )

    def delete_object(self, key: str) -> None:
        key = (key or "").lstrip("/")
        try:
            self._client.remove_object(self.bucket, key)
        except S3Error as e:
            if getattr(e, "code", "") in ("NoSuchKey", "NoSuchObject"):
                return
            raise
        except Exception:
            # treat delete as idempotent
            return
