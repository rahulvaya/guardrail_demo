"""Authenticated user identity passed between services."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Principal(BaseModel):
    """A verified user identity. Provider-agnostic.

    Concrete `IAuthProvider` implementations populate this from their native
    token format (Entra ID claims, Auth0 JWT, Cognito ID token, etc.).
    """

    subject: str = Field(..., description="Stable unique user id (sub claim).")
    username: str
    email: str | None = None
    tenant_id: str | None = None
    roles: list[str] = Field(default_factory=list)
    claims: dict[str, Any] = Field(
        default_factory=dict,
        description="Raw provider-specific claims, opaque to callers.",
    )
