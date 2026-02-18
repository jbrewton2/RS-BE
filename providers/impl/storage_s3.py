from __future__ import annotations

import os
from typing import Optional, Dict, Any

import boto3
from botocore.config import Config

from providers.storage import StorageProvider


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


class S3StorageProvider(StorageProvider):
    """
    Native AWS S3 StorageProvider (GovCloud target).

    Uses boto3 credential resolution (IRSA in EKS).
    No access keys required/expected in AWS runtime.

    Required env:
      - S3_BUCKET

    Optional env:
      - S3_PREFIX (e.g. "stores/" or "")

      - AWS_REGION or AWS_DEFAULT_REGION
      - S3_PRESIGN_TTL_SECONDS (default 900)
    """

    def __init__(self, bucket: str, prefix: str = "", region: Optional[str] = None):
        bucket = (bucket or "").strip()
        if not bucket:
            raise RuntimeError("S3_BUCKET is required for S3 storage provider")

        prefix = (prefix or "").strip()
        if prefix and not prefix.endswith("/"):
            prefix = prefix + "/"

        self.bucket = bucket
        self.prefix = prefix

        region = (region or _env("AWS_REGION") or _env("AWS_DEFAULT_REGION") or "").strip() or None

        # GovCloud-friendly config: retries + regional STS are typically already set in env
        cfg = Config(
            retries={"max_attempts": 8, "mode": "standard"},
            region_name=region,
        )
        self.s3 = boto3.client("s3", config=cfg)

    @classmethod
    def from_env(cls) -> "S3StorageProvider":
        bucket = _env("S3_BUCKET")
        prefix = _env("S3_PREFIX", "")
        region = _env("AWS_REGION") or _env("AWS_DEFAULT_REGION") or ""
        return cls(bucket=bucket, prefix=prefix, region=region or None)

    def _key(self, key: str) -> str:
        key = (key or "").lstrip("/")
        if self.prefix:
            return f"{self.prefix}{key}"
        return key

    def put_object(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: Optional[Dict[str, str]] = None,
    ) -> None:
        k = self._key(key)
        kwargs: Dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": k,
            "Body": data,
            "ContentType": content_type or "application/octet-stream",
        }
        if metadata:
            # S3 metadata keys must be strings
            kwargs["Metadata"] = {str(kk): str(vv) for kk, vv in metadata.items()}
        self.s3.put_object(**kwargs)

    def get_object(self, key: str) -> bytes:
        k = self._key(key)
        resp = self.s3.get_object(Bucket=self.bucket, Key=k)
        return resp["Body"].read()

    def head_object(self, key: str) -> Dict[str, Any]:
        k = self._key(key)
        resp = self.s3.head_object(Bucket=self.bucket, Key=k)
        # Return a stable dict (avoid dumping massive boto response)
        return {
            "ContentLength": resp.get("ContentLength"),
            "ContentType": resp.get("ContentType"),
            "ETag": resp.get("ETag"),
            "LastModified": resp.get("LastModified").isoformat() if resp.get("LastModified") else None,
            "Metadata": resp.get("Metadata") or {},
        }

    def delete_object(self, key: str) -> None:
        k = self._key(key)
        self.s3.delete_object(Bucket=self.bucket, Key=k)

    def presign_url(self, key: str, ttl_seconds: int = 900) -> str:
        k = self._key(key)
        ttl = ttl_seconds
        try:
            ttl = int(_env("S3_PRESIGN_TTL_SECONDS", str(ttl_seconds)) or ttl_seconds)
        except Exception:
            ttl = ttl_seconds

        return self.s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": self.bucket, "Key": k},
            ExpiresIn=max(1, ttl),
        )