# backend/auth/deps.py
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from .jwt import decode_and_verify_token, TokenData, require_role as _require_role

bearer_scheme = HTTPBearer(auto_error=True)

async def get_current_token(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> TokenData:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication scheme",
        )
    return decode_and_verify_token(credentials.credentials)

def require_realm_role(role: str):
    """Factory that returns a dependency enforcing a specific realm role."""

    async def _dependency(token: TokenData = Depends(get_current_token)) -> TokenData:
        _require_role(token, role)
        return token

    return _dependency
