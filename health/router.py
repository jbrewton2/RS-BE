# backend/health/router.py
from fastapi import APIRouter
import httpx

from core.settings import get_settings

router = APIRouter(tags=["health"])


# ----------------------------
# Basic Health
# ----------------------------

@router.get("/health", operation_id="health_root")
def health():
    # Keep this super simple and always unauthenticated
    return {"ok": True}


# ALIAS for hosted routing expectations (Front Door routes /api/*)
@router.get("/api/health", operation_id="health_api_health")
def api_health():
    # Same response as /health
    return {"ok": True}


# ----------------------------
# LLM Health
# ----------------------------


# ----------------------------
# DB Health (Postgres)
# ----------------------------
# NOTE: These are safe, read-only-ish checks. vector-health is idempotent

def _pg_connect():
    s = get_settings()
    host = s.db.host
    port = int(s.db.port)
    db = s.db.database
    user = s.db.user
    pw = s.db.password

    # Prefer psycopg (v3), fall back to psycopg2
    try:
        import psycopg  # type: ignore
        return psycopg.connect(host=endpoint, port=port, dbname=db, user=user, password=pw)
    except Exception:
        import psycopg2  # type: ignore
        return psycopg2.connect(host=endpoint, port=port, dbname=db, user=user, password=pw)


@router.get("/api/db/health")
def db_health():
    try:
        conn = _pg_connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1;")
            row = cur.fetchone()
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return {"ok": True, "db": True, "select1": row[0] if row else None}
    except Exception as e:
        return {"ok": False, "db": False, "error": str(e)}


@router.get("/api/db/vector-health")
def db_vector_health():
    """
    OpenSearch-only vector health check (config validation).
    No external calls (deterministic, zero-cost).
    """
    import os
    provider = (os.environ.get("VECTOR_STORE") or os.environ.get("VECTOR_PROVIDER") or "opensearch").strip().lower()
    endpoint = (os.environ.get("OPENSEARCH_ENDPOINT") or "").strip()
    index_name = (os.environ.get("OPENSEARCH_INDEX") or "").strip()
    region = (os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "").strip()

    ok = True
    missing = []
    if provider != "opensearch":
        ok = False
        missing.append("VECTOR_STORE=opensearch")
    if not endpoint:
        ok = False
        missing.append("OPENSEARCH_ENDPOINT")
    if not index_name:
        ok = False
        missing.append("OPENSEARCH_INDEX")
    if not region:
        ok = False
        missing.append("AWS_REGION or AWS_DEFAULT_REGION")

    return {
        "ok": ok,
        "provider": provider,
        "opensearch_endpoint": endpoint,
        "opensearch_index": index_name,
        "region": region,
        "missing": missing,
    }





