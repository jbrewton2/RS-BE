from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import boto3
from opensearchpy import OpenSearch, RequestsHttpConnection
from opensearchpy.helpers import bulk
from requests_aws4auth import AWS4Auth

from providers.vectorstore import VectorStore

logger = logging.getLogger(__name__)


def _is_opensearch_expired_token_error(e: Exception) -> bool:
    """
    Detect auth-expired / SigV4 failures that require client refresh.
    We match by message because opensearch-py may wrap errors differently.
    """
    msg = (str(e) or "").lower()
    if "security token included in the request is expired" in msg:
        return True
    if "authorizationexception" in msg and "403" in msg:
        return True
    if "the security token included in the request is expired" in msg:
        return True
    return False

def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    v = _env(name, "")
    if not v:
        return int(default)
    try:
        return int(v)
    except Exception as exc:
        raise RuntimeError(f"{name} must be an integer (got {v!r})") from exc


def _parse_host(endpoint: str) -> str:
    """
    Accept either:
      - https://vpc-...amazonaws.com
      - vpc-...amazonaws.com
    Return hostname (no scheme).
    """
    endpoint = (endpoint or "").strip()
    if not endpoint:
        raise RuntimeError("OPENSEARCH_ENDPOINT is missing")

    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        host = urlparse(endpoint).netloc
    else:
        host = endpoint

    host = host.strip()
    if not host:
        raise RuntimeError(f"OPENSEARCH_ENDPOINT is invalid: {endpoint!r}")

    return host


class OpenSearchVectorStore(VectorStore):
    """
    SigV4-signed OpenSearch vector store.

    Requires:
      - OPENSEARCH_ENDPOINT (https://... or hostname)
      - OPENSEARCH_INDEX (default: css_doc_chunks_1024)
      - OPENSEARCH_VECTOR_DIM (default: 1024)
      - AWS_REGION (or AWS_DEFAULT_REGION)
    """

    def __init__(self) -> None:
        self.endpoint = _env("OPENSEARCH_ENDPOINT")
        self.index = "css_doc_chunks_1024"
        self.dim = _env_int("OPENSEARCH_VECTOR_DIM", 1024)
        self.region = _env("AWS_REGION") or _env("AWS_DEFAULT_REGION")
        if not self.region:
            raise RuntimeError("AWS_REGION/AWS_DEFAULT_REGION is missing for OpenSearch SigV4")

        self.host = _parse_host(self.endpoint)

        # Build client from fresh creds (IRSA) â€“ can be refreshed on auth expiry
        self.client = self._build_client()

        self._ensure_index()

    def _build_client(self) -> OpenSearch:
        """
        Create a SigV4-signed OpenSearch client using *fresh* boto3 credentials.
        This is critical for long-lived pods where IRSA session tokens rotate.
        """
        sess = boto3.Session(region_name=self.region)
        creds = sess.get_credentials()
        if creds is None:
            raise RuntimeError("Failed to obtain AWS credentials for SigV4 (IRSA)")

        frozen = creds.get_frozen_credentials()
        awsauth = AWS4Auth(
            frozen.access_key,
            frozen.secret_key,
            self.region,
            "es",
            session_token=frozen.token,
        )

        return OpenSearch(
            hosts=[{"host": self.host, "port": 443}],
            http_auth=awsauth,
            use_ssl=True,
            verify_certs=True,
            connection_class=RequestsHttpConnection,
            timeout=30,
            max_retries=3,
            retry_on_timeout=True,
        )

    def _refresh_client(self) -> None:
        # Rebuild client from fresh creds (used on auth expiry)
        self.client = self._build_client()
    def _ensure_index(self) -> None:
# Index creation is safe/idempotent
        if self.client.indices.exists(index=self.index):
            return

        body = {
            "settings": {
                "index": {
                    "knn": True,
                    "number_of_shards": 1,
                    "number_of_replicas": 1,
                }
            },
            "mappings": {
                "properties": {
                    # IMPORTANT: used for retrieval scoping in RAG (filter by review_id)
                    "review_id": {"type": "keyword"},
                    "document_id": {"type": "keyword"},
                    "chunk_id": {"type": "keyword"},
                    "doc_name": {"type": "keyword"},
                    "chunk_text": {"type": "text"},
                    "meta": {"type": "object", "enabled": True},
                    "embedding": {"type": "knn_vector", "dimension": self.dim},
                }
            },
        }

        self.client.indices.create(index=self.index, body=body)

    @staticmethod
    def _extract_review_id(ch: Dict[str, Any]) -> str:
        """
        Fallback only (we prefer passing review_id explicitly from the request).

        Preference order:
          1) ch["review_id"] / ch["reviewId"]
          2) ch["meta"]["review_id"] / ch["meta"]["reviewId"]
        """
        meta = ch.get("meta") or {}
        rid = (
            ch.get("review_id")
            or ch.get("reviewId")
            or (meta.get("review_id") if isinstance(meta, dict) else None)
            or (meta.get("reviewId") if isinstance(meta, dict) else None)
            or ""
        )
        return str(rid).strip()

    def upsert_chunks(self, document_id: str, chunks: List[Dict[str, Any]], review_id: str = "") -> None:
        if not document_id or not chunks:
            return

        actions: List[Dict[str, Any]] = []

        for ch in chunks:
            chunk_id = str(ch.get("chunk_id") or ch.get("id") or "")
            if not chunk_id:
                continue

            emb = ch.get("embedding") or []
            if not isinstance(emb, list) or not emb:
                continue

            # Persist review_id for scoped retrieval (prefer request param; fallback to chunk/meta)
            rid = (review_id or self._extract_review_id(ch)).strip()

            doc = {
                "review_id": rid,
                "document_id": document_id,
                "chunk_id": chunk_id,
                "doc_name": str(ch.get("doc_name") or ch.get("doc") or ""),
                "chunk_text": str(ch.get("chunk_text") or ch.get("text") or ""),
                "meta": ch.get("meta") or {},
                "embedding": emb,
            }

            actions.append(
                {
                    "_op_type": "index",
                    "_index": self.index,
                    "_id": f"{document_id}::{chunk_id}",
                    "_source": doc,
                }
            )

        if not actions:
            return

        # Log what we are about to write (critical for debugging review_id scoping)
        try:
            rid0 = (actions[0].get("_source") or {}).get("review_id")
            logger.info(
                "[OpenSearch] upsert_chunks index=%s doc_id=%s actions=%s review_id=%s",
                self.index,
                document_id,
                len(actions),
                rid0,
            )
        except Exception:
            pass

        # Perform bulk write
        try:
            bulk(self.client, actions, refresh=True)
        except Exception as e:
            if _is_opensearch_expired_token_error(e):
                logger.warning("[OpenSearch] auth expired during bulk; refreshing client and retrying once")
                self._refresh_client()
                bulk(self.client, actions, refresh=True)
            else:
                raise

    def query(
        self,
        query_embedding: List[float],
        top_k: int = 10,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        self._ensure_index()
        if not query_embedding:
            return []

        k = max(1, min(int(top_k or 10), 50))

        # ---- harden embedding type (avoid VALUE_STRING in OpenSearch JSON) ----
        vec = query_embedding
        try:
            if isinstance(vec, str):
                import json as _json

                vec = _json.loads(vec)
            if isinstance(vec, tuple):
                vec = list(vec)
            vec = [float(x) for x in (vec or [])]
        except Exception:
            return []

        if not vec:
            return []

        # ---- term filters ----
        term_filters: List[dict] = []
        if filters:
            for fk, fv in filters.items():
                if fv is None or fv == "":
                    continue
                term_filters.append({"term": {str(fk): fv}})

        # ---- OpenSearch kNN query form ----
        body: Dict[str, Any] = {
            "size": k,
            "query": {
                "bool": {
                    "must": [
                        {
                            "knn": {
                                "embedding": {
                                    "vector": vec,
                                    "k": k,
                                }
                            }
                        }
                    ],
                    "filter": term_filters,
                }
            },
        }

        try:
            try:
                resp = self.client.search(index=self.index, body=body)
            except Exception as e:
                if _is_opensearch_expired_token_error(e):
                    logger.warning("[OpenSearch] auth expired during search; refreshing client and retrying once")
                    self._refresh_client()
                    resp = self.client.search(index=self.index, body=body)
                else:
                    raise
        except Exception as exc:
            # Do not silently return 0; this is a critical signal.
            logger.exception("[OpenSearch] search failed index=%s filters=%s", self.index, term_filters)
            raise

        hits = (resp or {}).get("hits", {}).get("hits", []) or []

        # Log hitcount and filters
        try:
            logger.info("[OpenSearch] query index=%s k=%s filters=%s hits=%s", self.index, k, term_filters, len(hits))
        except Exception:
            pass

        out: List[Dict[str, Any]] = []
        for h in hits:
            src = h.get("_source") or {}
            out.append(
                {
                    "review_id": src.get("review_id"),
                    "document_id": src.get("document_id"),
                    "chunk_id": src.get("chunk_id"),
                    "doc_name": src.get("doc_name"),
                    "chunk_text": src.get("chunk_text"),
                    "meta": src.get("meta") or {},
                    "score": h.get("_score"),
                }
            )

        return out

    def delete_by_document(self, document_id: str) -> None:
        if not document_id:
            return
        body = {"query": {"term": {"document_id": document_id}}}
        self.client.delete_by_query(index=self.index, body=body, refresh=True, conflicts="proceed")






