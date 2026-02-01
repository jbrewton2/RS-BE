from __future__ import annotations

import logging
from core.settings import get_settings

log = logging.getLogger(__name__)


class AuthConfigError(RuntimeError):
    pass


def validate_auth_config() -> None:
    """
    Validate auth-related configuration at startup.

    - Keycloak: local/dev only → no hard validation
    - Entra: warn on incomplete config
    - OIDC/Cognito: hard fail if JWKS URL missing
    """
    s = get_settings()
    provider = (s.auth.provider or "").lower()

    if provider == "keycloak":
        log.info("Auth provider: keycloak (local/dev)")
        return

    if provider == "entra":
        missing = []
        if not s.auth.entra.tenant_id and not s.auth.entra.authority:
            missing.append("ENTRA_TENANT_ID or ENTRA_AUTHORITY")
        if not s.auth.entra.audience_allowlist:
            missing.append("ENTRA_AUDIENCE_ALLOWLIST")

        if missing:
            log.warning(
                "Entra auth config incomplete. Missing: %s",
                ", ".join(missing),
            )
        return

    if provider in ("oidc", "cognito"):
        if not s.auth.oidc.jwks_url:
            raise AuthConfigError(
                "OIDC_JWKS_URL is required when AUTH_PROVIDER=oidc or cognito"
            )

        if not s.auth.oidc.issuer:
            log.warning(
                "OIDC_ISSUER not set. Issuer allowlist will be empty unless explicitly configured."
            )

        if not s.auth.oidc.audience_allowlist:
            log.info(
                "OIDC_AUDIENCE_ALLOWLIST not set. Audience will not be enforced."
            )

        log.info("Auth provider: %s (OIDC)", provider)
        return

    log.warning("Unknown AUTH_PROVIDER value: %s", provider)