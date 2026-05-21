"""Centralized observability: structured JSON logging + Prometheus metrics.

One module owns all telemetry so application code never reaches for
``logging`` directly. Call ``obs_log("event.name", level="info", **fields)``
and the JSON record will be enriched with whatever context variables
(``request_id``, ``tenant_id``, ``policy_id``, ``stage``) are active.

This service does NOT ship spans / traces itself. We emit one JSON log
line per significant event (one per request, one per stage, one per
guard decision) plus a Prometheus ``/metrics`` scrape endpoint. The
consumer is expected to forward those logs into whatever telemetry
stack they already run (ELK, Loki, Splunk, App Insights, etc.) and
scrape ``/metrics`` from their existing Prometheus/Grafana stack.

PII-safety contract
-------------------
NOTHING here logs raw user text. Callers that have a piece of user
content should pass it through ``text_fingerprint(...)`` and log the
returned ``{"text_sha256": ..., "text_len": ...}`` dict. The
``safe_reasons(...)`` helper truncates and hash-suffixes any free-form
reason strings so a guard's matched substring can't leak into the log
stream.

Optional deps
-------------
* ``prometheus-client``  -> if missing, metrics are no-ops and
                            ``metrics_response()`` returns 503.

This means the module is safe to import in a minimal environment
(unit tests, the bankbuddy in-tree harness) without dragging in the
full observability stack.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import sys
import time
import uuid
from typing import Any

# ---------------------------------------------------------------------------
# Context variables (request-scoped, propagated through asyncio)
# ---------------------------------------------------------------------------

_request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "guardrails_request_id", default=None
)
_tenant_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "guardrails_tenant_id", default=None
)
_policy_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "guardrails_policy_id", default=None
)
_stage_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "guardrails_stage", default=None
)


def set_request_context(
    *,
    request_id: str | None = None,
    tenant_id: str | None = None,
    policy_id: str | None = None,
    stage: str | None = None,
) -> dict[str, Any]:
    """Set the request-scoped context variables.

    Returns a dict of the tokens so callers can ``reset_request_context``
    on the way out of the request (not strictly required - the contextvar
    snapshot dies with the task - but tidy for long-lived loops).
    """
    tokens: dict[str, Any] = {}
    if request_id is not None:
        tokens["request_id"] = _request_id_var.set(request_id)
    if tenant_id is not None:
        tokens["tenant_id"] = _tenant_id_var.set(tenant_id)
    if policy_id is not None:
        tokens["policy_id"] = _policy_id_var.set(policy_id)
    if stage is not None:
        tokens["stage"] = _stage_var.set(stage)
    return tokens


def reset_request_context(tokens: dict[str, Any]) -> None:
    for key, tok in tokens.items():
        var = {
            "request_id": _request_id_var,
            "tenant_id": _tenant_id_var,
            "policy_id": _policy_id_var,
            "stage": _stage_var,
        }[key]
        try:
            var.reset(tok)
        except Exception:  # noqa: BLE001
            pass


def current_request_id() -> str | None:
    return _request_id_var.get()


def current_tenant_id() -> str | None:
    return _tenant_id_var.get()


# ---------------------------------------------------------------------------
# PII-safe helpers
# ---------------------------------------------------------------------------

def text_fingerprint(text: str) -> dict[str, Any]:
    """Return a non-reversible fingerprint of user text for logs/metrics."""
    if text is None:
        return {"text_sha256": None, "text_len": 0}
    b = text.encode("utf-8", errors="replace")
    return {
        "text_sha256": hashlib.sha256(b).hexdigest(),
        "text_len": len(text),
    }


def _short_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()[:12]


def safe_reasons(reasons: list[str] | None, *, max_len: int = 80) -> list[str]:
    """Sanitize free-form reason strings so they can be logged.

    A guard's ``reasons`` may quote the offending substring (e.g.
    ``banned-substrings`` echoes the matched phrase). We:

    * truncate to ``max_len`` chars,
    * suffix with a short sha256 so duplicate-but-truncated reasons
      are still groupable in log search.

    The full unmodified ``reasons`` list is still returned to the API
    caller via the response body - it just never enters the log stream.
    """
    if not reasons:
        return []
    out: list[str] = []
    for r in reasons:
        s = str(r)
        if len(s) <= max_len:
            out.append(s)
        else:
            out.append(f"{s[:max_len]}…[sha12={_short_hash(s)}]")
    return out


# ---------------------------------------------------------------------------
# Structured JSON logging
# ---------------------------------------------------------------------------

class _JsonFormatter(logging.Formatter):
    """Emit each log record as a single JSON line."""

    # Standard LogRecord attributes we *don't* want to copy into the
    # payload (because we either rename them or they're noise).
    _STD = {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": getattr(record, "event", record.getMessage()),
        }
        # Request-scoped context (only included if set).
        for key, var in (
            ("request_id", _request_id_var),
            ("tenant_id", _tenant_id_var),
            ("policy_id", _policy_id_var),
            ("stage", _stage_var),
        ):
            val = var.get()
            if val is not None:
                payload[key] = val
        # Any extra fields passed via ``extra={"fields": {...}}``.
        extras = getattr(record, "fields", None)
        if extras:
            for k, v in extras.items():
                if k not in payload:
                    payload[k] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        try:
            return json.dumps(payload, default=str, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            # Last-resort fallback so a bad field never kills logging.
            payload = {
                "ts": payload["ts"], "level": payload["level"],
                "logger": payload["logger"],
                "event": "log.serialize_error",
                "fallback_msg": record.getMessage()[:200],
            }
            return json.dumps(payload, ensure_ascii=False)


_LOGGING_CONFIGURED = False


def setup_logging(level: str | int = "INFO") -> None:
    """Install the JSON formatter on the root logger. Idempotent."""
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    # Replace any existing handlers so we always emit JSON.
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root.addHandler(handler)
    # Quiet down uvicorn's default text loggers (they go through root now).
    for noisy in ("uvicorn.access",):
        logging.getLogger(noisy).propagate = False
    _LOGGING_CONFIGURED = True


# Internal logger used by obs_log.
_LOG = logging.getLogger("guardrails")
_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


def obs_log(
    event: str,
    *,
    level: str = "info",
    exc_info: bool = False,
    **fields: Any,
) -> None:
    """Emit one structured event. The only logging API the app should use.

    ``event`` is a short dotted name (``check.completed``, ``guard.block``,
    ``policy.load_failed``) that downstream log search can group on.
    All other context goes in ``fields`` and lands as top-level JSON keys.
    NEVER pass raw user text - use ``text_fingerprint(...)`` first.
    """
    lvl = _LEVELS.get(level.lower(), logging.INFO)
    if not _LOG.isEnabledFor(lvl):
        return
    _LOG.log(
        lvl,
        event,
        extra={"event": event, "fields": fields},
        exc_info=exc_info,
    )


# ---------------------------------------------------------------------------
# Prometheus metrics (optional dep)
# ---------------------------------------------------------------------------

try:  # pragma: no cover - import guard
    from prometheus_client import (  # type: ignore
        CONTENT_TYPE_LATEST,
        REGISTRY,
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )

    _PROM_AVAILABLE = True
except Exception:  # noqa: BLE001
    _PROM_AVAILABLE = False
    CONTENT_TYPE_LATEST = "text/plain"  # type: ignore
    REGISTRY = None  # type: ignore
    CollectorRegistry = None  # type: ignore

    class _NoopMetric:
        def labels(self, *_a: Any, **_kw: Any) -> "_NoopMetric":
            return self

        def observe(self, *_a: Any, **_kw: Any) -> None: ...
        def inc(self, *_a: Any, **_kw: Any) -> None: ...
        def dec(self, *_a: Any, **_kw: Any) -> None: ...
        def set(self, *_a: Any, **_kw: Any) -> None: ...

    def Counter(*_a: Any, **_kw: Any) -> _NoopMetric:  # type: ignore[no-redef]
        return _NoopMetric()

    def Gauge(*_a: Any, **_kw: Any) -> _NoopMetric:  # type: ignore[no-redef]
        return _NoopMetric()

    def Histogram(*_a: Any, **_kw: Any) -> _NoopMetric:  # type: ignore[no-redef]
        return _NoopMetric()

    def generate_latest(*_a: Any, **_kw: Any) -> bytes:  # type: ignore[no-redef]
        return b"# prometheus-client not installed\n"


# Latency buckets tuned for in-process guards (sub-ms) up to slow Azure
# calls (multi-second).
_LATENCY_BUCKETS = (
    0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)

GUARD_DURATION = Histogram(
    "guardrails_guard_duration_seconds",
    "Per-guard execution time in seconds.",
    labelnames=("tenant", "policy", "stage", "guard", "decision"),
    buckets=_LATENCY_BUCKETS,
)

STAGE_DURATION = Histogram(
    "guardrails_stage_duration_seconds",
    "End-to-end stage pipeline time in seconds.",
    labelnames=("tenant", "policy", "stage", "decision"),
    buckets=_LATENCY_BUCKETS,
)

CHECK_TOTAL = Counter(
    "guardrails_check_total",
    "Total /v1/check requests.",
    labelnames=("tenant", "policy", "stage", "decision"),
)

BLOCK_CATEGORY_TOTAL = Counter(
    "guardrails_block_category_total",
    "Block decisions grouped by category code.",
    labelnames=("tenant", "policy", "stage", "category"),
)

GUARD_ERROR_TOTAL = Counter(
    "guardrails_guard_error_total",
    "Guard crashed (treated as ALLOW per pipeline contract).",
    labelnames=("tenant", "policy", "stage", "guard"),
)

OVERRIDE_CACHE_SIZE = Gauge(
    "guardrails_override_cache_size",
    "Current size of the per-request override pipeline LRU cache.",
)

CIRCUIT_BREAKER_STATE = Gauge(
    "guardrails_circuit_breaker_state",
    "Circuit breaker state per Azure dependency (0=closed, 1=half_open, 2=open).",
    labelnames=("dependency",),
)


def metrics_response() -> tuple[bytes, str, int]:
    """Return ``(payload, content_type, status_code)`` for ``GET /metrics``."""
    if not _PROM_AVAILABLE:
        return (b"prometheus-client not installed\n", "text/plain", 503)
    return (generate_latest(REGISTRY), CONTENT_TYPE_LATEST, 200)


def _label(value: Any) -> str:
    """Coerce a label value safely for Prometheus (string, non-empty)."""
    if value is None or value == "":
        return "unknown"
    s = str(value)
    # Trim absurdly long labels (Prometheus stores every distinct value).
    return s[:64]


def record_guard(
    *,
    guard: str,
    decision: str,
    duration_seconds: float,
    categories: list[str] | None = None,
) -> None:
    tenant = _label(_tenant_id_var.get())
    policy = _label(_policy_id_var.get())
    stage = _label(_stage_var.get())
    GUARD_DURATION.labels(tenant, policy, stage, _label(guard), _label(decision)).observe(
        max(duration_seconds, 0.0)
    )
    if decision == "block" and categories:
        for cat in categories:
            BLOCK_CATEGORY_TOTAL.labels(tenant, policy, stage, _label(cat)).inc()


def record_guard_error(*, guard: str) -> None:
    GUARD_ERROR_TOTAL.labels(
        _label(_tenant_id_var.get()),
        _label(_policy_id_var.get()),
        _label(_stage_var.get()),
        _label(guard),
    ).inc()


def record_stage(*, decision: str, duration_seconds: float) -> None:
    tenant = _label(_tenant_id_var.get())
    policy = _label(_policy_id_var.get())
    stage = _label(_stage_var.get())
    STAGE_DURATION.labels(tenant, policy, stage, _label(decision)).observe(
        max(duration_seconds, 0.0)
    )
    CHECK_TOTAL.labels(tenant, policy, stage, _label(decision)).inc()


def set_override_cache_size(size: int) -> None:
    OVERRIDE_CACHE_SIZE.set(size)


def set_circuit_state(dependency: str, state: str) -> None:
    code = {"closed": 0, "half_open": 1, "open": 2}.get(state, 0)
    CIRCUIT_BREAKER_STATE.labels(_label(dependency)).set(code)


# ---------------------------------------------------------------------------
# FastAPI middleware
# ---------------------------------------------------------------------------

async def tracing_middleware(request: Any, call_next: Any) -> Any:
    """Per-request middleware: assigns request_id, extracts tenant.

    Headers consumed (all optional):
      * ``X-Request-Id``    -> request id (generated if absent).
      * ``X-Tenant-Id``     -> tenant id (defaults to ``"default"``).

    The response includes ``X-Request-Id`` so callers can correlate.

    Note: this service does not emit spans. Consumers who want
    distributed tracing should add their own middleware that reads
    the inbound ``traceparent`` header and creates spans in their
    own telemetry stack; this middleware deliberately leaves trace
    propagation to the caller.
    """
    request_id = (
        request.headers.get("x-request-id")
        or request.headers.get("X-Request-Id")
        or str(uuid.uuid4())
    )
    tenant_id = (
        request.headers.get("x-tenant-id")
        or request.headers.get("X-Tenant-Id")
        or os.getenv("GUARDRAILS_DEFAULT_TENANT", "default")
    )
    tokens = set_request_context(request_id=request_id, tenant_id=tenant_id)

    try:
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response
    finally:
        reset_request_context(tokens)
