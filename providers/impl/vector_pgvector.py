from __future__ import annotations

import os
import json
from typing import Any, Dict, List, Optional

import psycopg2
from psycopg2.extras import Json

from providers.vectorstore import VectorStore


def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return default if v is None else str(v)


def _vector_literal(vec: List[float]) -> str:
    # pgvector accepts: '[1,2,3]'::vector
    return "[" + ",".join(f"{float(x):.8f}" for x in (vec or [])) + "]"


class PgVectorStore(VectorStore):
    """
    pgvector-backed VectorStore using table: css_doc_chunks

    Schema expectation:
      css_doc_chunks(
        document_id text,
        chunk_id text,
        doc_name text,
        chunk_text text,
        embedding vector(768),
        meta jsonb
      )
    """

    def __init__(self, dsn: Optional[str] = None):
        self.dsn = dsn or _env("PG_DSN", "").strip()
        if not self.dsn:
            # fallback: build DSN from standard PG vars
            host = _env("PGHOST", "postgres").strip()
            port = _env("PGPORT", "5432").strip()
            db = _env("PGDATABASE", "css").strip()
            user = _env("PGUSER", "cssadmin").strip()
            pwd = _env("PGPASSWORD", "css").strip()
            self.dsn = f"postgresql://{user}:{pwd}@{host}:{port}/{db}"

    def _conn(self):
        return psycopg2.connect(self.dsn)

    def upsert_chunks(self, document_id: str, chunks: List[Dict[str, Any]]) -> None:
        if not document_id or not chunks:
            return

        rows = []
        for ch in chunks:
            chunk_id = str(ch.get("chunk_id") or ch.get("id") or "")
            if not chunk_id:
                continue
            doc_name = ch.get("doc_name")
            chunk_text = str(ch.get("chunk_text") or ch.get("text") or "")
            emb = ch.get("embedding") or []
            meta = ch.get("meta") or {}
            rows.append((document_id, chunk_id, doc_name, chunk_text, _vector_literal(emb), Json(meta)))

        if not rows:
            return

        sql = """
        INSERT INTO css_doc_chunks (document_id, chunk_id, doc_name, chunk_text, embedding, meta)
        VALUES (%s, %s, %s, %s, %s::vector, %s)
        ON CONFLICT (document_id, chunk_id)
        DO UPDATE SET
          doc_name = EXCLUDED.doc_name,
          chunk_text = EXCLUDED.chunk_text,
          embedding = EXCLUDED.embedding,
          meta = EXCLUDED.meta;
        """

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.executemany(sql, rows)

    def upsert_embeddings(self, embeddings: List[Dict[str, Any]]) -> None:
        # Not used in our wiring. Keep for interface compatibility.
        return

    def query(
        self,
        query_embedding: List[float],
        top_k: int = 10,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        if not query_embedding:
            return []

        top_k = max(1, min(int(top_k or 10), 50))
        qv = _vector_literal(query_embedding)

        where = []
        params: List[Any] = []

        # Optional filters
        doc_id = None
        if filters:
            doc_id = filters.get("document_id") or filters.get("docId") or filters.get("doc_id")
        if doc_id:
            where.append("document_id = %s")
            params.append(str(doc_id))

        # Review scoping (RAG safety boundary)
        review_id = None
        if filters:
            review_id = filters.get("review_id") or filters.get("reviewId")
        if review_id:
            where.append("meta->>'review_id' = %s")
            params.append(str(review_id))

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        # cosine distance operator: <=>  (lower = closer)
        sql = f"""
        SELECT
          document_id,
          chunk_id,
          doc_name,
          chunk_text,
          meta,
          (1 - (embedding <=> %s::vector)) AS score
        FROM css_doc_chunks
        {where_sql}
        ORDER BY embedding <=> %s::vector
        LIMIT %s;
        """

        params = [qv] + params + [qv, top_k]

        out: List[Dict[str, Any]] = []
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                for row in cur.fetchall():
                    out.append(
                        {
                            "document_id": row[0],
                            "chunk_id": row[1],
                            "doc_name": row[2],
                            "chunk_text": row[3],
                            "meta": row[4] or {},
                            "score": float(row[5] or 0.0),
                        }
                    )
        return out

    def delete_by_document(self, document_id: str) -> None:
        if not document_id:
            return
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM css_doc_chunks WHERE document_id = %s", (document_id,))

