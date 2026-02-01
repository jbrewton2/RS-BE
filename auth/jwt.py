from __future__ import annotations

import time
from typing import Any, Dict, Optional, List, Tuple

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import jwt
from jose.exceptions import JWTError

from core.settings import get_settings

bearer = HTTPBearer(auto_error=False)

# Cache JWKS per-url (Keycloak vs Entra have different JWKS endpoints)
_JWKS_CACHE: Dict[str, Dict[str, Any]] = {}  # url -> {"jwks":..., "fetched_at":...}
_JWKS_TTL_SECONDS = 3600  # 1 hour


# -----------------------------------------------------------------------------
# Helpers (keep behavior identical)
# -----------------------------------------------------------------------------
def _split_scopes(value: str) -> List[str]:
    """
    Accept:
      - "scope1 scope2"
      - "scope1,scope2"
      - "scope1, scope2"
    """
    raw = (value or "").replace(",", " ").strip()
    return [x.strip() for x in raw.split() if x.strip()]


# -----------------------------------------------------------------------------
# Entra helpers (Azure AD / Entra ID)
# -----------------------------------------------------------------------------
def _entra_jwks_url(tenant_id: str, authority: str) -> str:
    """
    Entra v2 JWKS endpoint (no openid-configuration fetch required):
      https://login.microsoftonline.com/<tenant_id>/discovery/v2.0/keys

    We keep the exact behavior from the previous implementation:
      - prefer tenant_id
      - else try to parse from authority if possible
    """
    tid = (tenant_id or "").strip()
    if not tid:
        auth = (authority or "").strip()
        parts = auth.split("/")
        if len(parts) >= 4:
            tid = parts[3]
    if not tid:
        return ""
    return f"https://login.microsoftonline.com/{tid}/discovery/v2.0/keys"


def _entra_aud_ok(claims: Dict[str, Any], allowlist: List[str]) -> bool:
    """
    Entra access tokens:
      - aud is usually a string (app id URI or GUID)
    Accept if aud matches any item in allowlist.
    """
    aud = claims.get("aud")
    if not allowlist:
        return True  # no allowlist configured = don't block
    if isinstance(aud, str):
        return aud in allowlist
    if isinstance(aud, list):
        return any(a in allowlist for a in aud if isinstance(a, str))
    return False


def _entra_scopes_ok(claims: Dict[str, Any], required: List[str]) -> bool:
    """
    Entra delegated permissions usually arrive in "scp" claim (space-separated).
    """
    if not required:
        return True
    scp = claims.get("scp") or ""
    token_scopes = set(_split_scopes(str(scp)))
    return all(s in token_scopes for s in required)


# -----------------------------------------------------------------------------
# Keycloak helpers
# -----------------------------------------------------------------------------
def _keycloak_jwks_url(issuer: str) -> str:
    issuer = (issuer or "").rstrip("/")
    return f"{issuer}/protocol/openid-connect/certs"


def _keycloak_aud_ok(claims: Dict[str, Any], client_id: str) -> bool:
    """
    Keycloak audience patterns vary.
    Accept if:
      - aud == client_id (string)
      - aud contains client_id (list)
      - OR azp == client_id
    """
    aud = claims.get("aud")
    azp = claims.get("azp")

    if isinstance(aud, str) and aud == client_id:
        return True
    if isinstance(aud, list) and client_id in aud:
        return True
    if isinstance(azp, str) and azp == client_id:
        return True
    return False


# -----------------------------------------------------------------------------
# JWKS fetch + key selection
# -----------------------------------------------------------------------------
def _jwks_url_for_provider() -> Tuple[str, str]:
    """
    Returns (provider, jwks_url).
    """
    s = get_settings()
    prov = (s.auth.provider or "keycloak").strip().lower()

    if prov == "entra":
        url = _entra_jwks_url(s.auth.entra.tenant_id, s.auth.entra.authority)
        return prov, url

    # default keycloak
    return "keycloak", _keycloak_jwks_url(s.auth.keycloak.issuer)


async def _get_jwks(jwks_url: str) -> Dict[str, Any]:
    if not jwks_url:
        raise HTTPException(status_code=401, detail="Auth error: JWKS URL not configured")

    now = int(time.time())
    cached = _JWKS_CACHE.get(jwks_url)
    if cached and cached.get("jwks") and (now - int(cached.get("fetched_at", 0)) < _JWKS_TTL_SECONDS):
        return cached["jwks"]

    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(jwks_url)
        r.raise_for_status()
        jwks = r.json()

    _JWKS_CACHE[jwks_url] = {"jwks": jwks, "fetched_at": now}
    return jwks


def _pick_key(jwks: Dict[str, Any], kid: str) -> Optional[Dict[str, Any]]:
    for k in jwks.get("keys", []):
        if k.get("kid") == kid and (k.get("use") == "sig" or k.get("use") is None):
            return k
    return None


# -----------------------------------------------------------------------------
# Main auth dependency
# -----------------------------------------------------------------------------
async def get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
) -> Dict[str, Any]:
    if not creds or not creds.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
        )

    token = creds.credentials
    provider, jwks_url = _jwks_url_for_provider()
    s = get_settings()

    try:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if not kid:
            raise HTTPException(status_code=401, detail="Invalid token: missing kid")

        jwks = await _get_jwks(jwks_url)
        key = _pick_key(jwks, kid)
        if not key:
            raise HTTPException(status_code=401, detail="Invalid token: signing key not found")

        # Verify signature, but enforce issuer/audience ourselves (allowlists).
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            options={
                "verify_aud": False,
                "verify_iss": False,
            },
        )

        iss = (claims.get("iss") or "").rstrip("/")

        if provider == "entra":
            allowed_issuers = [x.rstrip("/") for x in (s.auth.entra.issuer_allowlist or []) if x]
            if allowed_issuers and iss not in allowed_issuers:
                raise HTTPException(status_code=401, detail=f"Invalid issuer: {iss}")

            audiences = s.auth.entra.audience_allowlist or []
            if not _entra_aud_ok(claims, audiences):
                raise HTTPException(status_code=401, detail="Invalid token audience")

            required_scopes = s.auth.entra.required_scopes or []
            if not _entra_scopes_ok(claims, required_scopes):
                raise HTTPException(status_code=401, detail="Missing required scopes")

            return claims

        # keycloak (default)
        client_id = s.auth.keycloak.client_id
        allowed_issuers = [x.rstrip("/") for x in (s.auth.keycloak.issuer_allowed or []) if x]
        if allowed_issuers and iss not in allowed_issuers:
            raise HTTPException(status_code=401, detail=f"Invalid issuer: {iss}")

        if not _keycloak_aud_ok(claims, client_id):
            raise HTTPException(status_code=401, detail="Invalid token audience")

        return claims

    except HTTPException:
        raise
    except JWTError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=401, detail=f"Auth error: {str(e)}")
    except Exception as e:
        # If you ever see: "Name or service not known" here, it means the code
        # is still trying to hit an internal hostname (e.g., keycloak) that
        # doesn't exist in the current environment.
        raise HTTPException(status_code=401, detail=f"Auth error: {str(e)}")