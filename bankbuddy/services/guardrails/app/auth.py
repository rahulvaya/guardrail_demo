"""Bearer-token authentication for the guardrails service."""
from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from .settings import get_settings


def require_bearer(authorization: str | None = Header(default=None)) -> None:
    """Validate the Authorization header against GUARDRAILS_INTERNAL_TOKEN.

    Constant-time comparison to avoid timing attacks. The service is
    bound to an internal Docker network and is unreachable from the
    host, so this is a defense-in-depth check rather than the only
    barrier.
    """
    expected = get_settings().internal_token
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    presented = authorization.split(" ", 1)[1].strip()
    if not secrets.compare_digest(presented, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid bearer token")
