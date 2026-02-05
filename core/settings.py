from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import List


def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return default if v is None else str(v)


def _env_float(name: str, default: float) -> float:
    raw = _env(name, "")
    if not raw.strip():
        return default
    try:
        return float(raw)
    except Exception:
        return default


def _env_int(name: str, default: int) -> int:
    raw = _env(name, "")
    if not raw.strip():
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _env_csv_floats(name: str, default: List[float]) -> List[float]:
    raw = _env(name, "")
    if not raw.strip():
        return default
    out: List[float] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(float(part))
        except Exception:
            continue
    return out or default


def _split_csv(value: str) -> List[str]:
    return [x.strip() for x in (value or "").split(",") if x.strip()]


def _split_scopes(value: str) -> List[str]:
    """
    Accept:
      - "scope1 scope2"
      - "scope1,scope2"
      - "scope1, scope2"
    """
    raw = (value or "").replace(",", " ").strip()
    return [x.strip() for x in raw.split() if x.strip()]


# ---------------------------------------------------------------------
# Settings models
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class LLMSettings:
    provider: str
    api_url: str
    model: str
    timeout_seconds: float
    connect_timeout_seconds: float
    max_attempts: int
    backoff_seconds: List[float]


@dataclass(frozen=True)
class StorageSettings:
    """
    Storage provider configuration.

    provider:
      - "local"  -> LocalFilesStorageProvider
      - "minio"  -> MinIO/S3-compatible object store provider
    """
    provider: str

    # Local
    local_dir: str = "./data"

    # S3-compatible (used when provider == "minio")
    minio_endpoint: str = "http://minio:9000"
    minio_bucket: str = "css"
    minio_access_key: str = ""
    minio_secret_key: str = ""


@dataclass(frozen=True)
class EntraAuthSettings:
    tenant_id: str
    authority: str
    issuer_allowlist: List[str]
    audience_allowlist: List[str]
    required_scopes: List[str]


@dataclass(frozen=True)
class KeycloakAuthSettings:
    issuer: str
    issuer_allowed: List[str]
    client_id: str
    client_id_allowlist: List[str]


@dataclass(frozen=True)
class OIDCAuthSettings:
    issuer: str
    jwks_url: str
    issuer_allowlist: List[str]
    audience_allowlist: List[str]
    required_scopes: List[str]


@dataclass(frozen=True)
class AuthSettings:
    provider: str
    entra: EntraAuthSettings
    keycloak: KeycloakAuthSettings
    oidc: OIDCAuthSettings


@dataclass(frozen=True)
class DBSettings:
    host: str
    port: int
    user: str
    password: str
    database: str


@dataclass(frozen=True)
class VectorSettings:
    provider: str  # disabled | pgvector | opensearch (future)


@dataclass(frozen=True)
class Settings:
    llm: LLMSettings
    storage: StorageSettings
    auth: AuthSettings
    db: DBSettings
    vector: VectorSettings


# ---------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------

def _load_db_settings() -> DBSettings:
    host = (_env("PGHOST", "localhost")).strip()
    port = _env_int("PGPORT", 5432)
    user = (_env("PGUSER", "postgres")).strip()
    password = _env("PGPASSWORD", "")
    database = (_env("PGDATABASE", "postgres")).strip()

    if port <= 0:
        port = 5432

    return DBSettings(host=host, port=int(port), user=user, password=password, database=database)


def _load_vector_settings() -> VectorSettings:
    provider = (_env("VECTOR_STORE", "") or _env("VECTOR_PROVIDER", "") or "disabled").strip().lower()
    return VectorSettings(provider=provider)


def _load_llm_settings() -> LLMSettings:
    provider = (_env("LLM_PROVIDER", "") or _env("OLLAMA_PROVIDER", "") or "ollama").strip().lower()

    api_url = (_env("LLM_API_URL", "") or "").strip()
    model = (_env("LLM_MODEL", "") or "").strip()

    timeout_seconds = _env_float("LLM_TIMEOUT_SECONDS", 240.0)
    connect_timeout_seconds = _env_float("LLM_CONNECT_TIMEOUT_SECONDS", 10.0)
    max_attempts = _env_int("LLM_MAX_ATTEMPTS", 2)
    backoff_seconds = _env_csv_floats("LLM_BACKOFF_SECONDS", [0.3])

    if not api_url:
        api_url = (_env("OLLAMA_API_URL", "") or "http://localhost:11434/api/generate").strip()
    if not model:
        model = (_env("OLLAMA_MODEL", "") or "llama3.1").strip()

    if "LLM_TIMEOUT_SECONDS" not in os.environ and "OLLAMA_TIMEOUT_SECONDS" in os.environ:
        timeout_seconds = _env_float("OLLAMA_TIMEOUT_SECONDS", timeout_seconds)
    if "LLM_MAX_ATTEMPTS" not in os.environ and "OLLAMA_MAX_ATTEMPTS" in os.environ:
        max_attempts = _env_int("OLLAMA_MAX_ATTEMPTS", max_attempts)
    if "LLM_BACKOFF_SECONDS" not in os.environ and "OLLAMA_BACKOFF_SECONDS" in os.environ:
        backoff_seconds = _env_csv_floats("OLLAMA_BACKOFF_SECONDS", backoff_seconds)
    if "LLM_CONNECT_TIMEOUT_SECONDS" not in os.environ and "OLLAMA_CONNECT_TIMEOUT_SECONDS" in os.environ:
        connect_timeout_seconds = _env_float("OLLAMA_CONNECT_TIMEOUT_SECONDS", connect_timeout_seconds)

    timeout_seconds = max(5.0, float(timeout_seconds))
    connect_timeout_seconds = max(1.0, float(connect_timeout_seconds))
    max_attempts = max(1, min(int(max_attempts), 5))
    backoff_seconds = [max(0.0, float(x)) for x in (backoff_seconds or [0.3])] or [0.3]

    return LLMSettings(
        provider=provider,
        api_url=api_url,
        model=model,
        timeout_seconds=timeout_seconds,
        connect_timeout_seconds=connect_timeout_seconds,
        max_attempts=max_attempts,
        backoff_seconds=backoff_seconds,
    )


def _normalize_storage_provider(raw: str) -> str:
    v = (raw or "").strip().lower()
    if v in ("minio", "s3", "object_store", "objectstore"):
        return "minio"
    if v in ("local", "file", "files", "filesystem"):
        return "local"
    return "local"


def _load_storage_settings() -> StorageSettings:
    """
    Storage precedence (DO NOT break this):
      1) STORAGE_MODE (deployment/runtime truth)  <-- must win
      2) STORAGE_PROVIDER (legacy override)
      3) default local
    """
    raw_mode = (_env("STORAGE_MODE", "") or "").strip()
    raw_provider = (_env("STORAGE_PROVIDER", "") or "").strip()
    provider = _normalize_storage_provider(raw_mode or raw_provider or "local")

    local_dir = (_env("STORAGE_LOCAL_DIR", "") or _env("LOCAL_STORAGE_DIR", "") or "./data").strip()

    minio_endpoint = (_env("MINIO_ENDPOINT", "") or "http://minio:9000").strip().rstrip("/")
    minio_bucket = (_env("MINIO_BUCKET", "") or "css").strip()
    minio_access_key = (_env("MINIO_ACCESS_KEY", "") or "").strip()
    minio_secret_key = (_env("MINIO_SECRET_KEY", "") or "").strip()

    return StorageSettings(
        provider=provider,
        local_dir=local_dir,
        minio_endpoint=minio_endpoint,
        minio_bucket=minio_bucket,
        minio_access_key=minio_access_key,
        minio_secret_key=minio_secret_key,
    )


def _load_entra_auth_settings() -> EntraAuthSettings:
    tenant_id = (_env("ENTRA_TENANT_ID", "") or "").strip()

    raw_authority = (_env("ENTRA_AUTHORITY", "") or "").strip()
    if raw_authority:
        authority = raw_authority.rstrip("/")
    elif tenant_id:
        authority = f"https://login.microsoftonline.com/{tenant_id}/v2.0"
    else:
        authority = ""

    raw_issuers = (_env("ENTRA_ISSUER_ALLOWLIST", "") or "").strip()
    if raw_issuers:
        issuer_allowlist = [x.rstrip("/") for x in _split_csv(raw_issuers)]
    else:
        issuer_allowlist = [authority] if authority else []

    raw_aud = (_env("ENTRA_AUDIENCE_ALLOWLIST", "") or "").strip()
    audience_allowlist = [x.strip() for x in _split_csv(raw_aud)]

    raw_scopes = (_env("ENTRA_REQUIRED_SCOPES", "") or "").strip()
    required_scopes = _split_scopes(raw_scopes)

    return EntraAuthSettings(
        tenant_id=tenant_id,
        authority=authority,
        issuer_allowlist=issuer_allowlist,
        audience_allowlist=audience_allowlist,
        required_scopes=required_scopes,
    )


def _load_keycloak_auth_settings() -> KeycloakAuthSettings:
    issuer = (
        (_env("AUTH_ISSUER", "") or _env("KEYCLOAK_ISSUER", "http://keycloak:8080/realms/css-local") or "")
    ).rstrip("/")

    # Allowlist from env (optional)
    raw_allowed = (_env("AUTH_ISSUER_ALLOWED", "") or _env("KEYCLOAK_ISSUER_ALLOWED", "") or "").strip()
    issuer_allowed = [x.rstrip("/") for x in _split_csv(raw_allowed) if x]

    # Always allow BOTH internal + external forms in local dev
    internal = ""
    external = ""

    if issuer.startswith("http://keycloak:8080"):
        internal = issuer
        external = issuer.replace("http://keycloak:8080", "http://localhost:8090")
    elif issuer.startswith("http://localhost:8090"):
        external = issuer
        internal = issuer.replace("http://localhost:8090", "http://keycloak:8080")
    elif issuer:
        internal = issuer  # unknown host, still allow it

    for cand in [internal, external]:
        if cand and cand not in issuer_allowed:
            issuer_allowed.append(cand)

    client_id = (_env("AUTH_CLIENT_ID", "") or _env("KEYCLOAK_CLIENT_ID", "css-frontend") or "css-frontend").strip()

    raw_allow = (_env("AUTH_CLIENT_ID_ALLOWLIST", "") or _env("KEYCLOAK_CLIENT_ID_ALLOWLIST", "") or "").strip()
    client_id_allowlist = [x.strip() for x in raw_allow.split(",") if x.strip()]

    return KeycloakAuthSettings(
        issuer=issuer,
        issuer_allowed=issuer_allowed,
        client_id=client_id,
        client_id_allowlist=client_id_allowlist,
    )


    # Issuer allowlist (service-agnostic preferred)
    raw_allowed = (_env("AUTH_ISSUER_ALLOWED", "") or _env("KEYCLOAK_ISSUER_ALLOWED", "") or "").strip()
    issuer_allowed = [x.rstrip("/") for x in _split_csv(raw_allowed) if x.strip()]

    # Dev convenience: if internal issuer is used, allow localhost issuer too
    if issuer and issuer.startswith("http://keycloak:8080"):
        external = issuer.replace("http://keycloak:8080", "http://localhost:8090")
        if external not in issuer_allowed:
            issuer_allowed.append(external)

    # Primary client id expected by backend (service-agnostic preferred)
    client_id = (_env("AUTH_CLIENT_ID", "") or _env("KEYCLOAK_CLIENT_ID", "css-frontend") or "css-frontend").strip()

    # Allowlist of additional client ids (e.g., SPA client)
    raw_allow = (_env("AUTH_CLIENT_ID_ALLOWLIST", "") or _env("KEYCLOAK_CLIENT_ID_ALLOWLIST", "") or "").strip()
    client_id_allowlist = [x.strip() for x in raw_allow.split(",") if x.strip()]

    return KeycloakAuthSettings(
        issuer=issuer,
        issuer_allowed=issuer_allowed,
        client_id=client_id,
        client_id_allowlist=client_id_allowlist,
    )


def _load_oidc_auth_settings() -> OIDCAuthSettings:
    issuer = (_env("OIDC_ISSUER", "") or "").strip().rstrip("/")
    jwks_url = (_env("OIDC_JWKS_URL", "") or "").strip()

    raw_issuers = (_env("OIDC_ISSUER_ALLOWLIST", "") or "").strip()
    if raw_issuers:
        issuer_allowlist = [x.rstrip("/") for x in _split_csv(raw_issuers)]
    else:
        issuer_allowlist = [issuer] if issuer else []

    raw_aud = (_env("OIDC_AUDIENCE_ALLOWLIST", "") or "").strip()
    audience_allowlist = [x.strip() for x in _split_csv(raw_aud)]

    raw_scopes = (_env("OIDC_REQUIRED_SCOPES", "") or "").strip()
    required_scopes = _split_scopes(raw_scopes)

    return OIDCAuthSettings(
        issuer=issuer,
        jwks_url=jwks_url,
        issuer_allowlist=issuer_allowlist,
        audience_allowlist=audience_allowlist,
        required_scopes=required_scopes,
    )


def _load_auth_settings() -> AuthSettings:
    provider = (_env("AUTH_PROVIDER", "") or "keycloak").strip().lower()
    return AuthSettings(
        provider=provider,
        entra=_load_entra_auth_settings(),
        keycloak=_load_keycloak_auth_settings(),
        oidc=_load_oidc_auth_settings(),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        llm=_load_llm_settings(),
        storage=_load_storage_settings(),
        auth=_load_auth_settings(),
        db=_load_db_settings(),
        vector=_load_vector_settings(),
    )
