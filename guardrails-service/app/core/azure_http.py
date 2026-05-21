"""Shared httpx.AsyncClient for outbound Azure calls.

All Azure-backed guards (Content Safety, Language PII, Groundedness,
Task Adherence) talk to the **same** Cognitive Services multi-service
resource. Before this module each guard owned its own
``httpx.AsyncClient`` with default settings, which meant:

* every guard paid its own TLS handshake on first call (~200-400 ms each
  -> ~1-1.5 s of avoidable cold-start when 3-4 guards run in a stage),
* parallel guards inside one stage opened separate TCP connections
  instead of multiplexing on one HTTP/2 stream,
* idle connections were dropped quickly, so a few seconds of silence
  re-paid TLS on the next prompt.

With one shared client tuned as below, parallel guards in a stage cost
~1 RTT instead of N, and the pool stays warm between prompts.
"""
from __future__ import annotations

import httpx

from .observability import obs_log

_client: httpx.AsyncClient | None = None

# Generous pool: bursts of parallel guard calls (3-4 per stage) plus
# multiple in-flight stages should never starve. Keep idle connections
# alive for 5 minutes so a quiet user gap doesn't force a TLS replay.
_LIMITS = httpx.Limits(
    max_connections=64,
    max_keepalive_connections=32,
    keepalive_expiry=300.0,
)


def get_client(timeout: float = 10.0) -> httpx.AsyncClient:
    """Return the process-wide shared async HTTP client.

    The first caller wins on timeout; later callers override per-request
    via ``client.post(..., timeout=...)`` if they need a tighter bound.
    HTTP/2 is enabled when the ``h2`` package is installed (it is in
    requirements.txt); otherwise httpx silently falls back to HTTP/1.1.
    """
    global _client
    if _client is None:
        try:
            _client = httpx.AsyncClient(
                timeout=timeout,
                http2=True,
                limits=_LIMITS,
            )
            obs_log(
                "azure_http.client_created",
                http2=True,
                max_connections=_LIMITS.max_connections,
                max_keepalive=_LIMITS.max_keepalive_connections,
                keepalive_expiry=_LIMITS.keepalive_expiry,
            )
        except Exception as e:  # noqa: BLE001
            # h2 missing or otherwise rejected -> fall back to HTTP/1.1.
            obs_log(
                "azure_http.http2_unavailable",
                level="warning",
                error_type=type(e).__name__,
            )
            _client = httpx.AsyncClient(timeout=timeout, limits=_LIMITS)
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def prewarm(endpoint: str) -> None:
    """Open one connection to ``endpoint`` so the first user prompt
    doesn't pay TLS. We don't care about the response code; we just want
    the TCP+TLS (and HTTP/2 SETTINGS) handshake out of the way.
    """
    if not endpoint:
        return
    client = get_client()
    try:
        # A bare GET to the resource root returns 404/401 quickly -- both
        # are fine, the connection is what we want.
        await client.get(endpoint.rstrip("/") + "/", timeout=5.0)
        obs_log("azure_http.prewarmed", endpoint=endpoint)
    except Exception as e:  # noqa: BLE001
        obs_log(
            "azure_http.prewarm_skipped",
            endpoint=endpoint,
            error_type=type(e).__name__,
        )
