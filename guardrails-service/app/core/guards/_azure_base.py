"""Shared plumbing for every Azure-backed guard.

Each Azure guard (Content Safety, Language PII, Groundedness, Task
Adherence, Topic Relevance) used to re-implement the same six blocks
of code: endpoint/key parsing, the shared HTTP client wrapper, the
sync+async auth-header path, an HTTP POST + error wrapper, and a
``_fail`` helper that renders the "skipped" category pills shown in
the UI. This module centralises all of that so subclasses only own
their guard-specific request payload and response parsing.

Public surface for subclasses:

    class MyGuard(AzureGuardBase):
        ENDPOINT_ENV_VARS = ("AZURE_FOO_ENDPOINT", "AZURE_CONTENT_SAFETY_ENDPOINT")
        KEY_ENV_VARS      = ("AZURE_FOO_KEY",      "AZURE_CONTENT_SAFETY_KEY")
        AAD_TOKEN_ENV_VARS = ("AZURE_FOO_AAD_TOKEN", "AZURE_CONTENT_SAFETY_AAD_TOKEN")
        CHECK_NAME = "text:foo"

        async def check(self, text, *, context=None):
            ok, headers = await self._prepare_request(text)
            if ok is not None:
                return ok                       # short-circuit (empty/no-endpoint/no-creds)
            body, err = await self._post_json(url, payload, headers=headers)
            if err:
                return self._fail_result(text, reason=err)
            ...
"""
from __future__ import annotations

import os
from typing import Any, ClassVar

import httpx

from ..aad_cache import get_bearer_token
from ..azure_http import get_client
from ..base import Guard, GuardCheckResult
from ..observability import obs_log
from .azure_endpoints import (
    COGNITIVE_SERVICES_AAD_SCOPE,
    CONTENT_SAFETY_API_VERSION,
)


def _first_env(names: tuple[str, ...]) -> str:
    """Return the first non-empty env var value from ``names`` (or "")."""
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return ""


def format_http_error(e: httpx.HTTPStatusError) -> str:
    """Uniform short HTTP error string used in guard reasons / metadata."""
    return f"HTTP {e.response.status_code}: {e.response.text[:200]}"


class AzureGuardBase(Guard):
    """Abstract-ish base for Azure-backed guards.

    Subclasses MUST override the class-level env-var tuples (or pass
    ``endpoint`` / ``api_key`` through config) and implement ``check``.
    """

    # --- Overridable class attributes -----------------------------------
    DEFAULT_API_VERSION: ClassVar[str] = CONTENT_SAFETY_API_VERSION
    AAD_SCOPE: ClassVar[str] = COGNITIVE_SERVICES_AAD_SCOPE
    ENDPOINT_ENV_VARS: ClassVar[tuple[str, ...]] = ("AZURE_CONTENT_SAFETY_ENDPOINT",)
    KEY_ENV_VARS: ClassVar[tuple[str, ...]] = ("AZURE_CONTENT_SAFETY_KEY",)
    AAD_TOKEN_ENV_VARS: ClassVar[tuple[str, ...]] = ("AZURE_CONTENT_SAFETY_AAD_TOKEN",)
    #: Short identifier surfaced in metadata["check"] when the request fails.
    CHECK_NAME: ClassVar[str] = "azure"

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)
        self.endpoint: str = (
            config.get("endpoint") or _first_env(self.ENDPOINT_ENV_VARS)
        ).rstrip("/")
        self.api_key: str = config.get("api_key") or _first_env(self.KEY_ENV_VARS)
        self.api_version: str = config.get("api_version", self.DEFAULT_API_VERSION)
        self.timeout_seconds: float = float(config.get("timeout_seconds", 5.0))
        self.fail_open: bool = bool(config.get("fail_open", True))

        if not self.endpoint:
            obs_log(
                "guard.azure.no_endpoint",
                level="warning",
                guard=self.name,
                env_vars=list(self.ENDPOINT_ENV_VARS),
                fail_mode="open" if self.fail_open else "closed",
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        """Return the shared process-wide HTTP client.

        The per-request timeout is applied in :meth:`_post_json` so that
        every guard can keep its own ``timeout_seconds`` even though the
        shared client's default timeout is fixed by the first caller.
        """
        return get_client(timeout=self.timeout_seconds)

    async def aclose(self) -> None:
        # The shared client is owned by core.azure_http and closed in
        # the FastAPI lifespan shutdown hook.
        return None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _auth_headers_sync(self) -> dict[str, str] | None:
        """Fast path: subscription key or env-supplied bearer token.

        Returns ``None`` when AAD must be used.
        """
        if self.api_key:
            return {"Ocp-Apim-Subscription-Key": self.api_key}
        token = _first_env(self.AAD_TOKEN_ENV_VARS)
        if token:
            return {"Authorization": f"Bearer {token}"}
        return None

    async def _auth_headers(self) -> dict[str, str]:
        """Resolve auth headers, falling back to the cached AAD token."""
        fast = self._auth_headers_sync()
        if fast is not None:
            return fast
        try:
            token = await get_bearer_token(self.AAD_SCOPE)
            if token:
                return {"Authorization": f"Bearer {token}"}
        except Exception as e:  # noqa: BLE001
            obs_log(
                "guard.azure.aad_unavailable",
                level="warning",
                guard=self.name,
                error_type=type(e).__name__,
            )
        return {}

    async def _prepare_request(
        self, text: str
    ) -> tuple[GuardCheckResult | None, dict[str, str]]:
        """Common pre-flight: empty-input / no-endpoint / no-creds.

        Returns ``(short_circuit_result, headers)``. When the first item
        is non-None the caller should return it immediately; otherwise
        ``headers`` is the dict to send with the request.
        """
        if not text or not text.strip():
            return self._allow(text), {}
        if not self.endpoint:
            return self._fail_result(text, reason="no endpoint configured"), {}
        headers = {"Content-Type": "application/json", **(await self._auth_headers())}
        if (
            "Ocp-Apim-Subscription-Key" not in headers
            and "Authorization" not in headers
        ):
            return self._fail_result(text, reason="no credentials available"), {}
        return None, headers

    # ------------------------------------------------------------------
    # HTTP helper
    # ------------------------------------------------------------------

    async def _post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str],
    ) -> tuple[dict[str, Any] | None, str | None]:
        """POST ``payload`` to ``url``. Returns ``(body, error)``.

        ``error`` is ``None`` on success. Per-request timeout follows
        ``self.timeout_seconds`` (independent of the shared client's
        default).
        """
        try:
            resp = await self._client().post(
                url, json=payload, headers=headers, timeout=self.timeout_seconds
            )
            resp.raise_for_status()
            return resp.json(), None
        except httpx.HTTPStatusError as e:
            return None, format_http_error(e)
        except Exception as e:  # noqa: BLE001
            return None, f"request error: {e!r}"

    # ------------------------------------------------------------------
    # Failure rendering
    # ------------------------------------------------------------------

    def _fail_result(
        self,
        text: str,
        *,
        reason: str,
        skipped_categories: list[dict[str, Any]] | None = None,
        extra_meta: dict[str, Any] | None = None,
    ) -> GuardCheckResult:
        """Render a unified fail-open / fail-closed result.

        ``skipped_categories`` lets a subclass surface the list of checks
        that would have run as "skipped" pills in the UI. ``extra_meta``
        is merged into the result metadata.
        """
        meta: dict[str, Any] = {
            "error": reason,
            "fail_open": self.fail_open,
            "check": "unavailable",
        }
        if skipped_categories is not None:
            meta["category_results"] = skipped_categories
        if extra_meta:
            meta.update(extra_meta)

        if self.fail_open:
            obs_log(
                "guard.azure.fail_open",
                level="warning",
                guard=self.name,
                reason=reason,
            )
            return self._allow(text, metadata=meta)
        obs_log(
            "guard.azure.fail_closed",
            level="warning",
            guard=self.name,
            reason=reason,
        )
        return self._block(
            text,
            reasons=[f"{self.name} unavailable: {reason}"],
            categories=["azure.unavailable"],
            metadata=meta,
        )

    @staticmethod
    def _skipped_pill(category: str, reason: str) -> dict[str, Any]:
        """Helper for building ``skipped_categories`` entries."""
        return {
            "category": category,
            "severity": None,
            "passed": None,
            "skipped": True,
            "reason": reason,
        }
