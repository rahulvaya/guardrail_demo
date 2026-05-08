"""Authentication provider interface.

Implementations:
    - LocalDevAuthProvider  (default; no real IdP, fake principal)
    - EntraAuthProvider     (Microsoft Entra ID / Azure AD)
    - Auth0Provider         (future)
    - CognitoProvider       (future, AWS)
    - KeycloakProvider      (future, on-prem / OSS)
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..contracts.principal import Principal


class AuthError(Exception):
    """Raised when authentication fails."""


class IAuthProvider(ABC):
    """OIDC-shaped authentication provider abstraction."""

    @abstractmethod
    def get_login_url(self, state: str) -> str:
        """Return the IdP authorization URL for the OIDC redirect flow."""

    @abstractmethod
    async def exchange_code(self, code: str) -> Principal:
        """Exchange an authorization code for a verified principal."""

    @abstractmethod
    async def verify_token(self, token: str) -> Principal:
        """Verify an access/ID token and return the principal.

        Raises:
            AuthError: if the token is invalid, expired, or untrusted.
        """
