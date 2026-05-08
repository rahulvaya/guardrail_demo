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

import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, status

from .api import CheckRequest, CheckResponse, GuardOutcome, PolicySummary
from .auth import require_bearer
from .core.pipeline import GuardrailPipeline
from .policies.loader import Policy, build_pipeline, load_policies
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
    try:
        yield
    finally:
        for p in pipelines.values():
            try:
                await p.aclose()
            except Exception:  # noqa: BLE001
                log.debug("error closing pipeline", exc_info=True)


app = FastAPI(title="BankBuddy Guardrails", version="1.0.0", lifespan=lifespan)


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
            input_guards=p.input_guard_names,
            output_guards=p.output_guard_names,
            tool_output_guards=p.tool_output_guard_names,
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
    pipeline: GuardrailPipeline | None = app.state.pipelines.get(pid)
    if pipeline is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"unknown policy: {pid}. Loaded: {sorted(app.state.pipelines.keys())}",
        )

    request_id = str(uuid.uuid4())
    started = time.perf_counter()

    if req.stage == "input":
        pr = await pipeline.check_input(req.text, context=req.context)
    elif req.stage == "tool_output":
        pr = await pipeline.check_tool_output(req.text, context=req.context)
    else:
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
