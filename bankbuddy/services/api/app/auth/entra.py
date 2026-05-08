"""Microsoft Entra ID (Azure AD) auth provider stub.

A real implementation uses MSAL's confidential client to perform the
authorization-code flow, then validates the resulting ID token against the
tenant's JWKS.

Phase 1 ships the wiring (settings, registration in factory) but the actual
token exchange and JWKS verification land in Phase 1d.1 once we have a
test tenant. Until then, calls raise `NotImplementedError`.
"""
from __future__ import annotations

from bankbuddy_shared.contracts.principal import Principal
from bankbuddy_shared.interfaces.auth import AuthError, IAuthProvider


class EntraAuthProvider(IAuthProvider):
    name = "entra"

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> None:
        if not all([tenant_id, client_id, client_secret, redirect_uri]):
            raise AuthError("entra provider requires tenant_id, client_id, client_secret, redirect_uri")
        self._tenant = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri

    def get_login_url(self, state: str) -> str:
        # Real impl: build via msal.ConfidentialClientApplication.get_authorization_request_url
        return (
            f"https://login.microsoftonline.com/{self._tenant}/oauth2/v2.0/authorize"
            f"?client_id={self._client_id}"
            f"&response_type=code"
            f"&redirect_uri={self._redirect_uri}"
            f"&scope=openid%20profile%20email"
            f"&state={state}"
        )

    async def exchange_code(self, code: str) -> Principal:  # noqa: ARG002
        raise NotImplementedError("EntraAuthProvider.exchange_code arrives in Phase 1d.1")

    async def verify_token(self, token: str) -> Principal:  # noqa: ARG002
        raise NotImplementedError("EntraAuthProvider.verify_token arrives in Phase 1d.1")
