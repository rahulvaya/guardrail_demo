"""Process-wide async-safe AAD bearer-token cache.

All Azure-backed guards (Content Safety, Language PII, Groundedness,
Task Adherence) hit the same Cognitive Services resource and therefore
need the same AAD token (scope ``https://cognitiveservices.azure.com/.default``).

Without caching, every guard call invokes ``DefaultAzureCredential.get_token``,
which on the first call performs an IMDS / client-credential round trip
that can cost 300-700 ms. Even subsequent calls (which the SDK does cache
internally) take a sync lock and check expiry, blocking the asyncio loop.

This module:

* keeps **one** ``DefaultAzureCredential`` per process,
* caches the token per scope until ``REFRESH_SKEW_SECONDS`` before its
  ``expires_on`` timestamp,
* coalesces concurrent refreshes on a per-scope ``asyncio.Lock`` so a
  burst of parallel guard calls triggers at most one refresh,
* offloads the sync ``get_token`` call to the default executor so the
  event loop stays responsive while the refresh is in flight.

Failures fall back to ``None`` so callers can decide whether to fail-open
or fail-closed; we deliberately do not raise here.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

log = logging.getLogger("guardrails.aad_cache")

REFRESH_SKEW_SECONDS = 300  # refresh 5 minutes before expiry

_credential: Any = None
_tokens: dict[str, tuple[str, float]] = {}  # scope -> (token, expires_on_epoch)
_locks: dict[str, asyncio.Lock] = {}


def _get_credential() -> Any:
    global _credential
    if _credential is None:
        # Imported lazily so test environments without azure.identity still load.
        from azure.identity import DefaultAzureCredential  # type: ignore

        _credential = DefaultAzureCredential()
    return _credential


def _lock_for(scope: str) -> asyncio.Lock:
    lock = _locks.get(scope)
    if lock is None:
        lock = asyncio.Lock()
        _locks[scope] = lock
    return lock


async def get_bearer_token(scope: str) -> str | None:
    """Return a valid cached AAD bearer token for ``scope``.

    Returns ``None`` when the credential chain cannot produce a token
    (e.g. running locally without ``az login`` / missing client secret).
    """
    now = time.time()
    cached = _tokens.get(scope)
    if cached is not None and cached[1] - REFRESH_SKEW_SECONDS > now:
        return cached[0]

    lock = _lock_for(scope)
    async with lock:
        # Re-check under the lock so concurrent waiters don't all refresh.
        cached = _tokens.get(scope)
        now = time.time()
        if cached is not None and cached[1] - REFRESH_SKEW_SECONDS > now:
            return cached[0]

        try:
            cred = _get_credential()
            loop = asyncio.get_running_loop()
            # ``get_token`` is sync and may block on network; run it in the
            # default executor to avoid stalling the event loop.
            access = await loop.run_in_executor(None, cred.get_token, scope)
        except Exception as e:  # noqa: BLE001
            log.warning("aad-cache: refresh failed scope=%s err=%r", scope, e)
            return None

        _tokens[scope] = (access.token, float(access.expires_on))
        log.info(
            "aad-cache: refreshed scope=%s ttl=%ds",
            scope,
            int(access.expires_on - now),
        )
        return access.token


def invalidate(scope: str | None = None) -> None:
    """Forget cached tokens. Useful when a 401 response indicates expiry."""
    if scope is None:
        _tokens.clear()
    else:
        _tokens.pop(scope, None)
