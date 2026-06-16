"""Guardrails service entrypoint.

Internal-only HTTP API exposed on port 8001. Every /v1/* request must
present a valid bearer token (GUARDRAILS_INTERNAL_TOKEN). The container
is bound to the `internal` Docker network and is unreachable from the
host - bearer auth is defense-in-depth.

Endpoints:
    GET  /healthz                       - liveness
    GET  /readyz                        - readiness (policies loaded)
    GET  /v1/policies                   - list available policies
    GET  /v1/policies/{id}              - inspect one policy
    POST /v1/check                      - run a stage of a policy on text
    GET  /v1/evaluate/evaluators        - list available evaluators (optionally filtered by stage)
    POST /v1/evaluate                   - run evaluators on a query/response pair
    POST /v1/evaluate/report            - same but returns a self-contained HTML report
    POST /v1/evaluate/batch             - run evaluators on multiple pairs in one call
    POST /v1/evaluate/batch/report      - same but returns a multi-item HTML report
"""
from __future__ import annotations

import json
import os
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status

from .api import CheckRequest, CheckResponse, GuardOutcome, PolicySummary
from .auth import require_bearer
from .core.observability import (
    current_request_id,
    metrics_response,
    obs_log,
    set_override_cache_size,
    setup_logging,
    text_fingerprint,
    tracing_middleware,
)
from .core.pipeline import GuardrailPipeline
from .policies.loader import (
    Policy,
    build_pipeline,
    build_pipeline_with_overrides,
    load_policies,
    validate_request_overrides,
)
from .evaluation import (
    EvaluateBatchRequest,
    EvaluateBatchResponse,
    EvaluateRequest,
    EvaluateResponse,
    EvaluatorInfo,
    format_batch_html_report,
    format_html_report,
    get_eval_settings,
    list_evaluators,
    run_batch_evaluation,
    run_evaluation,
)
from .settings import get_settings

# Install structured JSON logging on stdout. The service emits one
# JSON line per event (boot, policy load, check, guard decision). It
# does NOT ship spans/traces itself - consumers are expected to forward
# these logs into their own telemetry stack (ELK, Loki, App Insights, ...)
# and scrape `/metrics` from their existing Prometheus.
setup_logging(os.getenv("GUARDRAILS_LOG_LEVEL", "INFO"))

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    obs_log(
        "service.boot",
        policies_dir=str(settings.policies_dir),
        default_policy=settings.default_policy_id,
    )
    policies = load_policies(settings.policies_dir)
    pipelines: dict[str, GuardrailPipeline] = {}
    for pid, policy in policies.items():
        try:
            pipelines[pid] = build_pipeline(policy)
            obs_log("policy.pipeline_built", policy_id=pid)
        except Exception as e:  # noqa: BLE001
            obs_log(
                "policy.pipeline_build_failed",
                level="error",
                policy_id=pid,
                error_type=type(e).__name__,
                exc_info=True,
            )

    if settings.default_policy_id not in pipelines:
        obs_log(
            "policy.default_missing",
            level="error",
            default_policy=settings.default_policy_id,
        )

    app.state.policies = policies
    app.state.pipelines = pipelines
    # Cache for per-request override pipelines, keyed by
    # (policy_id, canonical-JSON of overrides). Bounded LRU so a noisy
    # caller that sends a fresh override per request can't grow this
    # without limit (each pipeline holds open httpx clients via its
    # guards' aclose()). Evicted entries are closed below in the
    # request handler.
    app.state.override_pipelines: "OrderedDict[Any, Any]" = OrderedDict()
    app.state.override_pipelines_max = int(
        os.getenv("GUARDRAILS_OVERRIDE_CACHE_MAX", "64")
    )
    set_override_cache_size(0)

    # Prewarm one TCP+TLS (+HTTP/2) connection to the Azure Cognitive
    # Services endpoint so the first user prompt doesn't pay handshake
    # latency across multiple guards.
    cs_endpoint = os.getenv("AZURE_CONTENT_SAFETY_ENDPOINT") or os.getenv("AZURE_LANGUAGE_ENDPOINT")
    if cs_endpoint:
        try:
            from .core.azure_http import prewarm
            await prewarm(cs_endpoint)
        except Exception:  # noqa: BLE001
            obs_log("azure_http.prewarm_failed", level="debug", exc_info=True)

    try:
        yield
    finally:
        for p in pipelines.values():
            try:
                await p.aclose()
            except Exception:  # noqa: BLE001
                obs_log("pipeline.close_failed", level="debug", exc_info=True)
        for p in app.state.override_pipelines.values():
            try:
                await p.aclose()
            except Exception:  # noqa: BLE001
                obs_log(
                    "override_pipeline.close_failed", level="debug", exc_info=True
                )
        try:
            from .core.azure_http import aclose as _azure_http_aclose
            await _azure_http_aclose()
        except Exception:  # noqa: BLE001
            obs_log("azure_http.close_failed", level="debug", exc_info=True)


app = FastAPI(title="Guardrails Service", version="1.0.0", lifespan=lifespan)
app.middleware("http")(tracing_middleware)


# ---------------------------------------------------------------------------
# Metrics  (Prometheus scrape endpoint, no auth - typical Prometheus setup)
# ---------------------------------------------------------------------------

@app.get("/metrics")
def metrics() -> Response:
    payload, content_type, code = metrics_response()
    return Response(content=payload, media_type=content_type, status_code=code)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": settings.service_name}


@app.get("/readyz")
def readyz() -> dict[str, Any]:
    pipelines = getattr(app.state, "pipelines", {})
    ready = settings.default_policy_id in pipelines
    payload = {
        "status": "ready" if ready else "degraded",
        "loaded_policies": sorted(pipelines.keys()),
        "default_policy": settings.default_policy_id,
    }
    if not ready:
        # 503 lets compose / k8s back off without flapping the container.
        return _json_response(503, payload)
    return payload


def _json_response(code: int, payload: dict[str, Any]) -> Any:
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=code, content=payload)


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------

@app.get("/v1/policies", dependencies=[Depends(require_bearer)])
def list_policies() -> dict[str, list[PolicySummary]]:
    items = [
        PolicySummary(
            id=p.id,
            description=p.description,
            api_input_guards=p.api_input_guard_names,
            input_guards=p.input_guard_names,
            tool_input_guards=p.tool_input_guard_names,
            output_guards=p.output_guard_names,
            tool_output_guards=p.tool_output_guard_names,
            api_output_guards=p.api_output_guard_names,
        )
        for p in app.state.policies.values()
    ]
    return {"policies": items}


@app.get("/v1/policies/{policy_id}", dependencies=[Depends(require_bearer)])
def get_policy(policy_id: str) -> dict[str, Any]:
    policy: Policy | None = app.state.policies.get(policy_id)
    if policy is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown policy: {policy_id}")
    return policy.raw


# ---------------------------------------------------------------------------
# Check
# ---------------------------------------------------------------------------

@app.post(
    "/v1/check",
    response_model=CheckResponse,
    dependencies=[Depends(require_bearer)],
)
async def check(req: CheckRequest) -> CheckResponse:
    pid = req.policy_id or settings.default_policy_id
    policy: Policy | None = app.state.policies.get(pid)
    if policy is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"unknown policy: {pid}. Loaded: {sorted(app.state.pipelines.keys())}",
        )

    # Choose pipeline: default (no overrides) or override-merged + cached.
    pipeline: GuardrailPipeline | None
    if req.overrides:
        if not settings.allow_request_overrides:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "per-request overrides are disabled (GUARDRAILS_ALLOW_REQUEST_OVERRIDES=false)",
            )
        errors = validate_request_overrides(
            req.overrides,
            policy,
            settings.overridable_keys_set(),
            settings.forbidden_override_keys_set(),
        )
        if errors:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, {"errors": errors})

        cache_key = (pid, json.dumps(req.overrides, sort_keys=True, default=str))
        cache: "OrderedDict[Any, Any]" = app.state.override_pipelines
        pipeline = cache.get(cache_key)
        if pipeline is None:
            pipeline = build_pipeline_with_overrides(policy, req.overrides)
            cache[cache_key] = pipeline
            # Evict LRU entries past the bound and close their guards so
            # we don't leak httpx clients / AAD tokens.
            while len(cache) > app.state.override_pipelines_max:
                _, evicted = cache.popitem(last=False)
                try:
                    await evicted.aclose()
                except Exception:  # noqa: BLE001
                    obs_log(
                        "override_pipeline.evict_close_failed",
                        level="debug",
                        exc_info=True,
                    )
            set_override_cache_size(len(cache))
            obs_log(
                "override_pipeline.built",
                policy_id=pid,
                override_keys=sorted(req.overrides.keys()),
            )
        else:
            # Cache hit: refresh recency.
            cache.move_to_end(cache_key)
    else:
        pipeline = app.state.pipelines.get(pid)
        if pipeline is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"policy {pid} known but pipeline failed to build at boot",
            )

    request_id = current_request_id() or ""
    started = time.perf_counter()

    # Aliases: callers may send llm_input/llm_output for clarity; map to the
    # canonical INPUT / OUTPUT pipelines so the existing 3-stage policy still
    # works without renames.
    stage = req.stage
    if stage == "llm_input":
        stage = "input"
    elif stage == "llm_output":
        stage = "output"

    # Publish request context for the duration of this call so every
    # downstream obs_log emitted by guards / pipeline carries policy_id
    # and the canonical stage tag automatically.
    from .core.observability import set_request_context as _set_ctx
    _set_ctx(policy_id=pid, stage=stage)

    if stage == "api_input":
        pr = await pipeline.check_api_input(req.text, context=req.context)
    elif stage == "input":
        pr = await pipeline.check_input(req.text, context=req.context)
    elif stage == "tool_input":
        pr = await pipeline.check_tool_input(req.text, context=req.context)
    elif stage == "tool_output":
        pr = await pipeline.check_tool_output(req.text, context=req.context)
    elif stage == "api_output":
        pr = await pipeline.check_api_output(req.text, context=req.context)
    else:  # "output"
        pr = await pipeline.check_output(req.text, context=req.context)

    decision: str
    if not pr.allowed:
        decision = "block"
    elif pr.was_modified:
        decision = "sanitize"
    else:
        decision = "allow"

    guards = [
        GuardOutcome(
            name=c.guard_name,
            decision=c.decision.value,
            reasons=c.reasons,
            categories=c.categories,
            score=c.score,
            metadata=c.metadata,
        )
        for c in pr.checks
    ]

    log_fields: dict[str, Any] = {
        "policy_id": pid,
        "requested_stage": req.stage,
        "stage": stage,
        "decision": decision,
        "duration_ms": round(pr.duration_ms, 2),
        "guard_count": len(guards),
        "block_categories": pr.block_categories,
        "override_keys": sorted(req.overrides.keys()) if req.overrides else [],
    }
    log_fields.update(text_fingerprint(req.text))
    obs_log("check.completed", **log_fields)

    return CheckResponse(
        decision=decision,  # type: ignore[arg-type]
        sanitized_text=pr.sanitized_text,
        stage=req.stage,
        policy_id=pid,
        duration_ms=round(pr.duration_ms or ((time.perf_counter() - started) * 1000.0), 2),
        block_reasons=pr.block_reasons,
        block_categories=pr.block_categories,
        guards=guards,
        request_id=request_id,
    )


# ---------------------------------------------------------------------------
# Evaluate
# ---------------------------------------------------------------------------

@app.get(
    "/v1/evaluate/evaluators",
    dependencies=[Depends(require_bearer)],
    summary="List available evaluators",
    description=(
        "Return all evaluators, optionally filtered by `stage`. "
        "The `available` flag shows whether required credentials are configured."
    ),
)
def get_evaluators(
    stage: str | None = None,
) -> dict[str, list[EvaluatorInfo]]:
    return {"evaluators": list_evaluators(stage, get_eval_settings())}


@app.post(
    "/v1/evaluate",
    response_model=EvaluateResponse,
    dependencies=[Depends(require_bearer)],
    summary="Evaluate a query/response pair",
    description=(
        "Run Azure AI Evaluation metrics on a query/response pair at any guardrail "
        "pipeline stage. Returns structured results with pass/fail per evaluator."
    ),
)
async def evaluate(req: EvaluateRequest) -> EvaluateResponse:
    return await run_evaluation(req, get_eval_settings())


@app.post(
    "/v1/evaluate/report",
    dependencies=[Depends(require_bearer)],
    summary="Evaluate and return an HTML report",
    description=(
        "Same as POST /v1/evaluate but returns a self-contained HTML page with "
        "colour-coded results tables for easy visual inspection."
    ),
    response_class=__import__('fastapi.responses', fromlist=['HTMLResponse']).HTMLResponse,
)
async def evaluate_report(req: EvaluateRequest):
    from fastapi.responses import HTMLResponse
    result = await run_evaluation(req, get_eval_settings())
    return HTMLResponse(content=format_html_report(result))


@app.post(
    "/v1/evaluate/batch",
    response_model=EvaluateBatchResponse,
    dependencies=[Depends(require_bearer)],
    summary="Batch-evaluate multiple query/response pairs",
    description=(
        "Evaluate multiple query/response pairs in a single request. "
        "All items are evaluated concurrently. "
        "Set a top-level `evaluators` list as the default for every item, "
        "or set per-item `evaluators` to override it. "
        "Use `GET /v1/evaluate/evaluators` to discover evaluator names."
    ),
)
async def evaluate_batch(req: EvaluateBatchRequest) -> EvaluateBatchResponse:
    return await run_batch_evaluation(req, get_eval_settings())


@app.post(
    "/v1/evaluate/batch/report",
    dependencies=[Depends(require_bearer)],
    summary="Batch-evaluate and return an HTML report",
    description=(
        "Same as POST /v1/evaluate/batch but returns a self-contained HTML page "
        "with per-item colour-coded results tables for easy visual inspection."
    ),
    response_class=__import__('fastapi.responses', fromlist=['HTMLResponse']).HTMLResponse,
)
async def evaluate_batch_report(req: EvaluateBatchRequest):
    from fastapi.responses import HTMLResponse
    result = await run_batch_evaluation(req, get_eval_settings())
    return HTMLResponse(content=format_batch_html_report(result))
