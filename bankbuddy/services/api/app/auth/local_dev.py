"""Local-dev auth provider.

No real identity provider. Returns one of two demo principals based on the
`code` value (`alice` or `bob`). For any other code, returns Alice. Used
when running BankBuddy locally without an IdP.

NEVER enable in production. The startup banner logs a clear warning when
this provider is selected.
"""
from __future__ import annotations

from bankbuddy_shared.contracts.principal import Principal
from bankbuddy_shared.interfaces.auth import IAuthProvider


_PRINCIPALS: dict[str, Principal] = {
    "alice": Principal(
        subject="cust-alice",
        username="alice",
        email="alice@example.com",
        tenant_id="local-dev",
        roles=["customer"],
        claims={"demo": True},
    ),
    "bob": Principal(
        subject="cust-bob",
        username="bob",
        email="bob@example.com",
        tenant_id="local-dev",
        roles=["customer"],
        claims={"demo": True},
    ),
}


class LocalDevAuthProvider(IAuthProvider):
    name = "local-dev"

    def get_login_url(self, state: str) -> str:  # noqa: ARG002
        # The UI's "login" page renders a username picker instead of redirecting.
        return f"/auth/local-dev?state={state}"

    async def exchange_code(self, code: str) -> Principal:
        return _PRINCIPALS.get(code.strip().lower(), _PRINCIPALS["alice"])

    async def verify_token(self, token: str) -> Principal:
        # The local-dev provider issues opaque tokens of the form `local::<username>`.
        if token.startswith("local::"):
            return await self.exchange_code(token[len("local::"):])
        return _PRINCIPALS["alice"]
