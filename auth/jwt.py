# backend/auth/jwt.py
import json
from functools import lru_cache
from typing import Any, Dict, List

import httpx
from jose import jwt, JWTError
from pydantic import BaseModel
from fastapi import HTTPException, status

from backend.core.config import SETTINGS  # if you have a settings object; otherwise use env directly

class TokenData(BaseModel):
    sub: str
    preferred_username: str | None = None
    email: str | None = None
    realm_roles: List[str] = []
    resource_roles: Dict[str, List[str]] = {}

class KeycloakConfig(BaseModel):
    url: str
    realm: str
    client_id: str

def get_keycloak_config() -> KeycloakConfig:
    # Adapt this to however you store config.
    # Option A: use a SETTINGS object; Option B: environment vars directly.
    return KeycloakConfig(
        url=SETTINGS.KEYCLOAK_URL,          # e.g. "http://localhost:8080"
        realm=SETTINGS.KEYCLOAK_REALM,      # "css-local"
        client_id=SETTINGS.KEYCLOAK_CLIENT_ID,  # "css-frontend"
    )

@lru_cache(maxsize=1)
def get_jwks() -> Dict[str, Any]:
    cfg = get_keycloak_config()
    jwks_url = f"{cfg.url}/realms/{cfg.realm}/protocol/openid-connect/certs"
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(jwks_url)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        raise RuntimeError(f"Failed to fetch Keycloak JWKS: {e}")

def decode_and_verify_token(token: str) -> TokenData:
    cfg = get_keycloak_config()
    jwks = get_jwks()
    unverified_header = jwt.get_unverified_header(token)
    kid = unverified_header.get("kid")

    key = None
    for jwk in jwks.get("keys", []):
        if jwk.get("kid") == kid:
            key = jwk
            break
    if key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token: unknown key id",
        )

    try:
        payload = jwt.decode(
            token,
            key,
            algorithms=[key.get("alg", "RS256")],
            audience=cfg.client_id,
            issuer=f"{cfg.url}/realms/{cfg.realm}",
        )
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Could not validate credentials: {e}",
        )

    # Extract roles from Keycloak-style claims
    realm_roles: List[str] = []
    resource_roles: Dict[str, List[str]] = {}

    realm_access = payload.get("realm_access") or {}
    if isinstance(realm_access, dict):
        roles = realm_access.get("roles") or []
        if isinstance(roles, list):
            realm_roles = [str(r) for r in roles]

    resource_access = payload.get("resource_access") or {}
    if isinstance(resource_access, dict):
        for client, data in resource_access.items():
            roles = data.get("roles") or []
            resource_roles[client] = [str(r) for r in roles]

    return TokenData(
        sub=str(payload.get("sub")),
        preferred_username=payload.get("preferred_username"),
        email=payload.get("email"),
        realm_roles=realm_roles,
        resource_roles=resource_roles,
    )

def require_role(token: TokenData, role: str) -> None:
    """Raise 403 if the Keycloak user does not have the given realm role."""
    if role not in token.realm_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing required role: {role}",
        )
