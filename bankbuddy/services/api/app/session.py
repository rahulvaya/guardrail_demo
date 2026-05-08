"""App-issued session JWT.

The browser only ever sees this token - never the raw IdP token. This
keeps the wire format stable when we swap auth providers.
"""
from __future__ import annotations

import time
from typing import Any

from bankbuddy_shared.contracts.principal import Principal
from jose import JWTError, jwt

from .settings import Settings


def issue_session_jwt(settings: Settings, principal: Principal) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": principal.subject,
        "username": principal.username,
        "email": principal.email,
        "tenant_id": principal.tenant_id,
        "roles": principal.roles,
        "iat": now,
        "exp": now + settings.app_jwt_ttl_seconds,
    }
    return jwt.encode(payload, settings.app_jwt_secret, algorithm=settings.app_jwt_algorithm)


def decode_session_jwt(settings: Settings, token: str) -> Principal:
    try:
        payload = jwt.decode(token, settings.app_jwt_secret, algorithms=[settings.app_jwt_algorithm])
    except JWTError as e:
        raise ValueError(f"invalid session token: {e}") from e
    return Principal(
        subject=payload["sub"],
        username=payload.get("username"),
        email=payload.get("email"),
        tenant_id=payload.get("tenant_id"),
        roles=list(payload.get("roles") or []),
        claims={},
    )
