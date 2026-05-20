"""Guardrails service entrypoint.

Internal-only HTTP API exposed on port 8001. Every /v1/* request must
present a valid bearer token (GUARDRAILS_INTERNAL_TOKEN). The container
is bound to the `internal` Docker network and is unreachable from the
host - bearer auth is defense-in-depth.

Endpoints:
    GET  /healthz                 - liveness
    GET  /readyz                  - readiness (policies loaded)
    GET  /v1/policies             - list available policies
    GET  /v1/policies/{id}        - inspect one policy
    POST /v1/check                - run a stage of a policy on text
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, status

from .api import CheckRequest, CheckResponse, GuardOutcome, PolicySummary
from .auth import require_bearer
from .core.pipeline import GuardrailPipeline
from .policies.loader import (
    Policy,
    build_pipeline,
    build_pipeline_with_overrides,
    load_policies,
    validate_request_overrides,
)
from .settings import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("guardrails")

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("guardrails service boot: policies_dir=%s default=%s",
             settings.policies_dir, settings.default_policy_id)
    policies = load_policies(settings.policies_dir)
    pipelines: dict[str, GuardrailPipeline] = {}
    for pid, policy in policies.items():
        try:
            pipelines[pid] = build_pipeline(policy)
            log.info("built pipeline for policy %s", pid)
        except Exception as e:  # noqa: BLE001
            log.exception("failed to build pipeline for %s: %s", pid, e)

    if settings.default_policy_id not in pipelines:
        log.error(
            "default policy %s not loaded; service will only accept explicit policy_id",
            settings.default_policy_id,
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

    # Prewarm one TCP+TLS (+HTTP/2) connection to the Azure Cognitive
    # Services endpoint so the first user prompt doesn't pay handshake
    # latency across multiple guards.
    cs_endpoint = os.getenv("AZURE_CONTENT_SAFETY_ENDPOINT") or os.getenv("AZURE_LANGUAGE_ENDPOINT")
    if cs_endpoint:
        try:
            from .core.azure_http import prewarm
            await prewarm(cs_endpoint)
        except Exception:  # noqa: BLE001
            log.debug("azure-http prewarm failed", exc_info=True)

    try:
        yield
    finally:
        for p in pipelines.values():
            try:
                await p.aclose()
            except Exception:  # noqa: BLE001
                log.debug("error closing pipeline", exc_info=True)
        for p in app.state.override_pipelines.values():
            try:
                await p.aclose()
            except Exception:  # noqa: BLE001
                log.debug("error closing override pipeline", exc_info=True)
        try:
            from .core.azure_http import aclose as _azure_http_aclose
            await _azure_http_aclose()
        except Exception:  # noqa: BLE001
            log.debug("error closing shared azure http client", exc_info=True)


app = FastAPI(title="Guardrails Service", version="1.0.0", lifespan=lifespan)


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
                    log.debug("error closing evicted override pipeline", exc_info=True)
            log.info("built override pipeline for policy=%s overrides=%s", pid, req.overrides)
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

    request_id = str(uuid.uuid4())
    started = time.perf_counter()

    # Aliases: callers may send llm_input/llm_output for clarity; map to the
    # canonical INPUT / OUTPUT pipelines so the existing 3-stage policy still
    # works without renames.
    stage = req.stage
    if stage == "llm_input":
        stage = "input"
    elif stage == "llm_output":
        stage = "output"

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

    log.info(
        "check policy=%s stage=%s decision=%s duration_ms=%.1f request_id=%s",
        pid, req.stage, decision, pr.duration_ms, request_id,
    )

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
