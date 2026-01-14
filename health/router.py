# backend/health/router.py
from fastapi import APIRouter
import os
import httpx

router = APIRouter(tags=["health"])

# ----------------------------
# Basic Health
# ----------------------------

@router.get("/health")
def health():
    # Keep this super simple and always unauthenticated
    return {"ok": True}

# ALIAS for hosted routing expectations (Front Door routes /api/*)
@router.get("/api/health")
def api_health():
    # Same response as /health
    return {"ok": True}

# ----------------------------
# LLM Health (Ollama)
# ----------------------------

@router.get("/health/llm")
async def health_llm():
    """
    Verifies:
      - Ollama is reachable
      - configured model exists in ollama tags
    """
    ollama_base = os.getenv("OLLAMA_BASE_URL")  # optional (if you want)
    ollama_api_url = os.getenv("OLLAMA_API_URL", "http://ollama:11434/api/generate")
    model = os.getenv("OLLAMA_MODEL", "llama3.1:8b-instruct-q4_K_M")

    # derive base URL from OLLAMA_API_URL if base not provided
    # e.g. http://ollama:11434/api/generate -> http://ollama:11434
    base = ollama_base or ollama_api_url.split("/api/")[0].rstrip("/")

    tags_url = f"{base}/api/tags"

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
    model_ready = model in models

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
    host = os.getenv("PGHOST")
    port = int(os.getenv("PGPORT", "5432"))
    db = os.getenv("PGDATABASE")
    user = os.getenv("PGUSER")
    pw = os.getenv("PGPASSWORD")

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
            cur.execute("SELECT '[1,2,3]'::vector;")
            row = cur.fetchone()
        finally:
            try:
                conn.close()
            except Exception:
                pass

        return {"ok": True, "pgvector": True, "sample_vector": str(row[0]) if row else None}
    except Exception as e:
        return {"ok": False, "pgvector": False, "error": str(e)}
