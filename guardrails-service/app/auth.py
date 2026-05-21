"""Bearer-token authentication for the guardrails service.

Two modes are supported, controlled by ``GUARDRAILS_AUTH_MODE``:

* ``static``                  - validate against ``GUARDRAILS_INTERNAL_TOKEN`` only.
* ``aad``                     - validate Microsoft Entra ID (Azure AD) JWTs only.
* ``static_or_aad`` (default) - accept either. The static token is intended
  as a break-glass / dev path; AAD JWTs are the recommended production
  mechanism (e.g. via Workload Identity).

AAD configuration is fully env-driven (see ``settings.py``):

* ``GUARDRAILS_AAD_TENANT_ID``      - tenant GUID (required for AAD).
* ``GUARDRAILS_AAD_AUDIENCE``       - expected ``aud`` claim, typically
  ``api://<app-id>`` or the app-id GUID (required for AAD).
* ``GUARDRAILS_AAD_ALLOWED_APPIDS`` - optional CSV of caller app-id GUIDs.
  When unset, any token from the configured tenant + audience is accepted.
* ``GUARDRAILS_AAD_ISSUER``         - optional issuer override
  (default: ``https://login.microsoftonline.com/<tenant>/v2.0``).
* ``GUARDRAILS_AAD_JWKS_URI``       - optional JWKS URL override
  (default: discovered via the tenant's OIDC document).
"""
from __future__ import annotations

import secrets
import time
from typing import Any

import httpx
from fastapi import Header, HTTPException, status

from .core.observability import obs_log
from .settings import get_settings

try:  # PyJWT is required for AAD mode; static-only deployments can omit it.
    import jwt
    from jwt import PyJWKClient

    _PYJWT_AVAILABLE = True
except Exception:  # pragma: no cover - exercised at import time
    jwt = None  # type: ignore[assignment]
    PyJWKClient = None  # type: ignore[assignment]
    _PYJWT_AVAILABLE = False


# ---------------------------------------------------------------------------
# JWKS / discovery cache
# ---------------------------------------------------------------------------

_DISCOVERY_CACHE_TTL_S = 3600
_discovery_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_jwks_clients: dict[str, Any] = {}


def _openid_config_url(tenant_id: str) -> str:
    return f"https://login.microsoftonline.com/{tenant_id}/v2.0/.well-known/openid-configuration"


def _fetch_openid_config(tenant_id: str) -> dict[str, Any]:
    cached = _discovery_cache.get(tenant_id)
    now = time.time()
    if cached and now - cached[0] < _DISCOVERY_CACHE_TTL_S:
        return cached[1]
    url = _openid_config_url(tenant_id)
    obs_log("auth.aad_discovery_fetch", tenant_id=tenant_id, url=url)
    r = httpx.get(url, timeout=10.0)
    r.raise_for_status()
    doc = r.json()
    _discovery_cache[tenant_id] = (now, doc)
    return doc


def _jwks_client_for(jwks_uri: str) -> Any:
    client = _jwks_clients.get(jwks_uri)
    if client is None:
        client = PyJWKClient(jwks_uri, cache_keys=True, lifespan=3600)
        _jwks_clients[jwks_uri] = client
    return client


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def _check_static_token(presented: str) -> bool:
    expected = get_settings().internal_token
    if not expected:
        return False
    return secrets.compare_digest(presented, expected)


def _check_aad_token(presented: str) -> None:
    """Validate an AAD-issued JWT. Raises HTTPException on failure."""
    if not _PYJWT_AVAILABLE:
        obs_log("auth.aad_pyjwt_missing", level="error")
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "AAD auth not available: install PyJWT[crypto]",
        )

    s = get_settings()
    tenant_id = s.aad_tenant_id
    audience = s.aad_audience
    if not tenant_id or not audience:
        obs_log(
            "auth.aad_misconfigured",
            level="error",
            tenant_id_set=bool(tenant_id),
            audience_set=bool(audience),
        )
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "guardrails AAD auth misconfigured (tenant_id / audience missing)",
        )

    try:
        if s.aad_issuer and s.aad_jwks_uri:
            issuer = s.aad_issuer
            jwks_uri = s.aad_jwks_uri
        else:
            cfg = _fetch_openid_config(tenant_id)
            issuer = s.aad_issuer or cfg["issuer"]
            jwks_uri = s.aad_jwks_uri or cfg["jwks_uri"]

        signing_key = _jwks_client_for(jwks_uri).get_signing_key_from_jwt(presented).key
        claims = jwt.decode(
            presented,
            signing_key,
            algorithms=["RS256"],
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "iss", "aud"]},
        )
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        obs_log(
            "auth.aad_jwt_invalid",
            level="warning",
            error_type=type(e).__name__,
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid AAD token") from e

    allowed = [a.strip() for a in (s.aad_allowed_appids or "").split(",") if a.strip()]
    if allowed:
        appid = claims.get("appid") or claims.get("azp")
        if appid not in allowed:
            obs_log(
                "auth.aad_appid_rejected",
                level="warning",
                appid=str(appid) if appid else None,
            )
            raise HTTPException(status.HTTP_403_FORBIDDEN, "caller not authorized")


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


def require_bearer(authorization: str | None = Header(default=None)) -> None:
    """Validate the ``Authorization`` header per the configured auth mode."""
    s = get_settings()
    mode = (s.auth_mode or "static_or_aad").lower()

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    presented = authorization.split(" ", 1)[1].strip()
    if not presented:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")

    if mode == "static":
        if not _check_static_token(presented):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid bearer token")
        return

    if mode == "aad":
        _check_aad_token(presented)
        return

    # Default: accept either. Try the cheap static check first.
    if _check_static_token(presented):
        return
    # Only attempt AAD validation if it is actually configured; otherwise
    # this is a plain invalid static token, not a server misconfig.
    if s.aad_tenant_id and s.aad_audience:
        _check_aad_token(presented)
        return
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid bearer token")
