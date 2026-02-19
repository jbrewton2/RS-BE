from __future__ import annotations

import os
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import boto3
from opensearchpy import OpenSearch, RequestsHttpConnection
from opensearchpy.helpers import bulk
from requests_aws4auth import AWS4Auth

from providers.vectorstore import VectorStore


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _parse_host(endpoint: str) -> str:
    endpoint = (endpoint or "").strip()
    if not endpoint:
        raise RuntimeError("OPENSEARCH_ENDPOINT is missing")
    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        return urlparse(endpoint).netloc
    return endpoint


class OpenSearchVectorStore(VectorStore):
    """
    SigV4-signed OpenSearch vector store.

    Requires:
      - OPENSEARCH_ENDPOINT
      - OPENSEARCH_INDEX
      - OPENSEARCH_VECTOR_DIM
      - AWS_REGION (or AWS_DEFAULT_REGION)
    """

    def __init__(self) -> None:
        self.endpoint = _env("OPENSEARCH_ENDPOINT")
        self.index = _env("OPENSEARCH_INDEX", "css_doc_chunks_1024")
        self.dim = int(_env("OPENSEARCH_VECTOR_DIM", "1024") or "1024")

        region = _env("AWS_REGION") or _env("AWS_DEFAULT_REGION")
        if not region:
            raise RuntimeError("AWS_REGION/AWS_DEFAULT_REGION is missing for OpenSearch signer")

        sess = boto3.Session(region_name=region)
        creds = sess.get_credentials()
        if creds is None:
            raise RuntimeError("Failed to obtain AWS credentials for SigV4 (IRSA)")

        frozen = creds.get_frozen_credentials()
        awsauth = AWS4Auth(
            frozen.access_key,
            frozen.secret_key,
            region,
            "es",
            session_token=frozen.token,
        )

        host = _parse_host(self.endpoint)

        self.client = OpenSearch(
            hosts=[{"host": host, "port": 443}],
            http_auth=awsauth,
            use_ssl=True,
            verify_certs=True,
            connection_class=RequestsHttpConnection,
            timeout=30,
            max_retries=3,
            retry_on_timeout=True,
        )

        self._ensure_index()

    def _ensure_index(self) -> None:
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
                    "document_id": {"type": "keyword"},
                    "chunk_id": {"type": "keyword"},
                    "doc_name": {"type": "keyword"},
                    "chunk_text": {"type": "text"},
                    "meta": {"type": "object", "enabled": True},
                    "embedding": {
                        "type": "knn_vector",
                        "dimension": self.dim,
                    },
                }
            },
        }

        self.client.indices.create(index=self.index, body=body)

    def upsert_chunks(self, document_id: str, chunks: List[Dict[str, Any]]) -> None:
        if not document_id or not chunks:
            return

        actions = []
        for ch in chunks:
            chunk_id = str(ch.get("chunk_id") or ch.get("id") or "")
            if not chunk_id:
                continue

            emb = ch.get("embedding") or []
            if not isinstance(emb, list) or not emb:
                continue

            doc = {
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

        bulk(self.client, actions, refresh=True)

    def query(
        self,
        query_embedding: List[float],
        top_k: int = 10,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        if not query_embedding:
            return []

        k = max(1, min(int(top_k or 10), 50))

        must_filters: List[dict] = []
        if filters:
            for fk, fv in filters.items():
                if fv is None or fv == "":
                    continue
                must_filters.append({"term": {fk: fv}})

        body: Dict[str, Any] = {
            "size": k,
            "query": {
                "bool": {
                    "must": must_filters,
                    "filter": [
                        {
                            "knn": {
                                "embedding": {
                                    "vector": query_embedding,
                                    "k": k,
                                }
                            }
                        }
                    ],
                }
            },
        }

        resp = self.client.search(index=self.index, body=body)
        hits = (resp or {}).get("hits", {}).get("hits", []) or []

        out: List[Dict[str, Any]] = []
        for h in hits:
            src = h.get("_source") or {}
            out.append(
                {
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