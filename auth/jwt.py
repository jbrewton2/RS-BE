from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional, List

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import jwt
from jose.exceptions import JWTError

bearer = HTTPBearer(auto_error=False)

_JWKS_CACHE: Dict[str, Any] = {"jwks": None, "fetched_at": 0}
_JWKS_TTL_SECONDS = 3600  # cache JWKS for 1 hour


def _jwks_issuer() -> str:
    """
    Used ONLY for fetching JWKS from inside docker network.
    This should usually be the internal docker URL.
    """
    return os.getenv("KEYCLOAK_ISSUER", "http://keycloak:8080/realms/css-local").rstrip("/")


def _allowed_issuers() -> List[str]:
    """
    Token issuer must match one of these EXACTLY.

    Default behavior (if KEYCLOAK_ISSUER_ALLOWED is not set):
      - accept internal docker issuer (http://keycloak:8080/realms/...)
      - accept the browser-facing issuer (http://localhost:8090/realms/...)

    You can override explicitly with:
      KEYCLOAK_ISSUER_ALLOWED="issuer1,issuer2"
    """
    raw = os.getenv("KEYCLOAK_ISSUER_ALLOWED", "").strip()
    if raw:
        return [x.strip().rstrip("/") for x in raw.split(",") if x.strip()]

    internal = _jwks_issuer()
    external = internal.replace("http://keycloak:8080", "http://localhost:8090")
    # de-dup and keep stable order
    out = []
    for x in [internal, external]:
        if x and x not in out:
            out.append(x)
    return out


def _jwks_url() -> str:
    return f"{_jwks_issuer()}/protocol/openid-connect/certs"


def _client_id() -> str:
    # your Keycloak client in realm
    return os.getenv("KEYCLOAK_CLIENT_ID", "css-frontend")


async def _get_jwks() -> Dict[str, Any]:
    now = int(time.time())
    if _JWKS_CACHE["jwks"] and (now - _JWKS_CACHE["fetched_at"] < _JWKS_TTL_SECONDS):
        return _JWKS_CACHE["jwks"]

    url = _jwks_url()
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        jwks = r.json()

    _JWKS_CACHE["jwks"] = jwks
    _JWKS_CACHE["fetched_at"] = now
    return jwks


def _pick_key(jwks: Dict[str, Any], kid: str) -> Optional[Dict[str, Any]]:
    for k in jwks.get("keys", []):
        if k.get("kid") == kid and k.get("use") == "sig":
            return k
    return None


def _aud_ok(claims: Dict[str, Any], client_id: str) -> bool:
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


async def get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
) -> Dict[str, Any]:
    if not creds or not creds.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
        )

    token = creds.credentials
    client_id = _client_id()
    allowed_issuers = [x.rstrip("/") for x in _allowed_issuers()]

    try:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if not kid:
            raise HTTPException(status_code=401, detail="Token missing kid")

        jwks = await _get_jwks()
        key = _pick_key(jwks, kid)
        if not key:
            raise HTTPException(status_code=401, detail="Signing key not found")

        # Decode with signature validation, but we enforce issuer ourselves (allowlist)
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            options={
                "verify_aud": False,  # we validate audience ourselves
                "verify_iss": False,  # we validate issuer ourselves (allowlist)
            },
        )

        iss = (claims.get("iss") or "").rstrip("/")
        if iss not in allowed_issuers:
            raise HTTPException(status_code=401, detail=f"Invalid issuer: {iss}")

        if not _aud_ok(claims, client_id):
            raise HTTPException(status_code=401, detail="Invalid token audience")

        return claims

    except HTTPException:
        raise
    except JWTError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Auth error: {str(e)}")
