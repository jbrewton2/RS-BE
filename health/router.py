# backend/health/router.py
from fastapi import APIRouter, Request
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
        return psycopg.connect(host=host, port=port, dbname=db, user=user, password=pw)
    except Exception:
        import psycopg2  # type: ignore
        return psycopg2.connect(host=host, port=port, dbname=db, user=user, password=pw)


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







# ---------------------------------------------------------------------
# Dependency Health Gate (deterministic)
# ---------------------------------------------------------------------

@router.get("/api/health/deps", include_in_schema=True)
def api_health_deps(request: Request):
    """
    Deterministic dependency gate used by UI before:
      - Create Review
      - Run AI / Analyze

    Checks:
      - AWS credentials available (IRSA)
      - OpenSearch reachable with SigV4 signing
    """
    import os
    import json
    import urllib.request
    from botocore.session import Session
    from botocore.awsrequest import AWSRequest
    from botocore.auth import SigV4Auth

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or ""
    endpoint = (os.environ.get("OPENSEARCH_ENDPOINT") or "").strip().rstrip("/")

    out = {
        "ok": True,
        "aws": {"ok": True, "region": region or None},
        "opensearch": {"ok": True, "endpoint": endpoint or None, "status": None, "error": None},
    }

    # ---- AWS creds (IRSA) ----
    sess = Session()
    creds = sess.get_credentials()
    if creds is None:
        out["ok"] = False
        out["aws"]["ok"] = False
        out["opensearch"]["ok"] = False
        out["opensearch"]["error"] = "no_aws_creds"
        return out

    if not region:
        out["ok"] = False
        out["aws"]["ok"] = False
        out["aws"]["error"] = "missing_aws_region"
        return out

    if not endpoint:
        out["ok"] = False
        out["opensearch"]["ok"] = False
        out["opensearch"]["error"] = "missing_opensearch_endpoint"
        return out

    # ---- SigV4 GET / ----
    host = endpoint.replace("https://", "").replace("http://", "")
    url = endpoint + "/"

    try:
        req = AWSRequest(method="GET", url=url, headers={"host": host})
        SigV4Auth(creds, "es", region).add_auth(req)
        signed_headers = dict(req.headers.items())

        r = urllib.request.Request(url, headers=signed_headers, method="GET")
        with urllib.request.urlopen(r, timeout=10) as resp:
            out["opensearch"]["status"] = int(resp.status)
            if int(resp.status) != 200:
                out["ok"] = False
                out["opensearch"]["ok"] = False
    except Exception as e:
        out["ok"] = False
        out["opensearch"]["ok"] = False
        out["opensearch"]["error"] = repr(e)

    return out