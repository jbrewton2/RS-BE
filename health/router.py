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

@router.get("/health/llm")
async def health_llm():
    """
    Verifies:
      - LLM endpoint is reachable
      - configured model exists in tags

    NOTE: Behavior preserved from prior Ollama-only implementation:
      - base URL derived from api_url by splitting at "/api/"
      - tags endpoint is "{base}/api/tags"
    """
    s = get_settings()

    # Preserve semantics: prefer configured api_url and model from canonical settings.
    # (Previously these were env-driven defaults.)
    api_url = (s.llm.api_url or "").strip()
    model = (s.llm.model or "").strip()

    # Derive base URL from api_url
    # e.g. http://ollama:11434/api/generate -> http://ollama:11434
    base = ""
    if api_url and "/api/" in api_url:
        base = api_url.split("/api/")[0].rstrip("/")
    elif api_url:
        base = api_url.rstrip("/")

    tags_url = f"{base}/api/tags" if base else ""

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(tags_url)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        return {
            "ok": False,
            "llmReachable": False,
            "modelReady": False,
            "model": model,
            "error": str(e),
        }

    models = [m.get("name") for m in data.get("models", []) if isinstance(m, dict)]
    model_ready = model in models if model else False

    return {
        "ok": True,
        "llmReachable": True,
        "modelReady": model_ready,
        "model": model,
        "knownModels": models[:25],  # keep response bounded
    }


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
    try:
        conn = _pg_connect()
        try:
            cur = conn.cursor()
            # Ensure pgvector is installed (idempotent)
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            try:
                conn.commit()
            except Exception:
                pass

            # Prove the type exists
            cur.execute("SELECT ''[1,2,3]''::vector;")
            row = cur.fetchone()
        finally:
            try:
                conn.close()
            except Exception:
                pass

        return {"ok": True, "pgvector": True, "sample_vector": str(row[0]) if row else None}
    except Exception as e:
        return {"ok": False, "pgvector": False, "error": str(e)}
