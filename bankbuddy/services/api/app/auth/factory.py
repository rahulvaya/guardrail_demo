"""Auth provider factory."""
from __future__ import annotations

import logging

from bankbuddy_shared.interfaces.auth import IAuthProvider

from ..settings import Settings
from .entra import EntraAuthProvider
from .local_dev import LocalDevAuthProvider

log = logging.getLogger("auth.factory")


def build_auth(settings: Settings) -> IAuthProvider:
    name = settings.auth_provider.lower()
    if name == "local-dev":
        log.warning("AUTH_PROVIDER=local-dev: insecure demo auth, do NOT use in production")
        return LocalDevAuthProvider()
    if name == "entra":
        return EntraAuthProvider(
            tenant_id=settings.entra_tenant_id or "",
            client_id=settings.entra_client_id or "",
            client_secret=settings.entra_client_secret or "",
            redirect_uri=settings.entra_redirect_uri or "",
        )
    raise ValueError(f"unknown AUTH_PROVIDER: {settings.auth_provider}")
