"""Agent service entrypoint.

The agent is **internal-only**: every endpoint other than `/health` requires
the `X-Internal-Token` header to match `AGENT_INTERNAL_TOKEN`. The service is
also bound only to the `internal` Docker network in compose, so even a
misconfigured caller cannot reach it from the host.
"""
from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from typing import Any

from bankbuddy_shared.contracts.agent import AgentInvokeRequest, AgentInvokeResponse
from bankbuddy_shared.interfaces.agent import AgentError
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel

from .banking.factory import build_banking
from .guardrails_client import RemoteGuardrailPipeline
from .llm.factory import build_llm
from .providers.factory import build_agent
from .settings import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("agent")

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "agent boot: provider=%s llm=%s/%s banking=%s guardrails=%s",
        settings.agent_provider,
        settings.llm_provider,
        settings.llm_model,
        settings.banking_backend,
        settings.guardrails_enabled,
    )
    banking = build_banking(settings)
    llm = build_llm(settings)
    log.info(
        "guardrails: remote service url=%s policy=%s",
        settings.guardrails_url, settings.guardrails_policy_id,
    )
    pipeline = RemoteGuardrailPipeline(
        base_url=settings.guardrails_url,
        token=settings.guardrails_internal_token,
        policy_id=settings.guardrails_policy_id,
        timeout_seconds=settings.guardrails_timeout_seconds,
        block_message=settings.guardrails_block_message,
    )
    await pipeline.warmup()
    app.state.guardrails = pipeline
    app.state.agent = build_agent(settings, llm=llm, banking=banking, guardrails=pipeline)
    log.info(
        "guardrails: input=%s output=%s",
        [g.name for g in pipeline.input_guards],
        [g.name for g in pipeline.output_guards],
    )
    try:
        yield
    finally:
        await pipeline.aclose()


app = FastAPI(title="BankBuddy Agent", version="0.2.0", lifespan=lifespan)


def _require_internal_token(x_internal_token: str | None = Header(default=None, alias="X-Internal-Token")) -> None:
    expected = settings.internal_token
    if not x_internal_token or not secrets.compare_digest(x_internal_token, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid internal token")


@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": settings.service_name,
        "provider": settings.agent_provider,
        "guardrails": "on" if settings.guardrails_enabled else "off",
    }


@app.get("/internal/ping", dependencies=[Depends(_require_internal_token)])
def ping() -> dict[str, str]:
    return {"pong": "ok"}


@app.post(
    "/internal/invoke",
    response_model=AgentInvokeResponse,
    dependencies=[Depends(_require_internal_token)],
)
async def invoke(request: AgentInvokeRequest) -> AgentInvokeResponse:
    try:
        return await app.state.agent.invoke(request)
    except AgentError as e:
        log.warning("agent error: %s", e)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e


# ---------------------------------------------------------------------------
# Guardrail introspection / debug endpoints
# ---------------------------------------------------------------------------


@app.get("/internal/guardrails/list", dependencies=[Depends(_require_internal_token)])
def list_guards() -> dict[str, Any]:
    """Return the active pipeline + all registered guard names."""
    pipeline = app.state.guardrails
    return {
        "master_enabled": settings.guardrails_enabled,
        "mode": "remote",
        "input_guards": [
            {"name": g.name, "stage": g.stage.value, "description": g.description, "config": g.config}
            for g in pipeline.input_guards
        ],
        "output_guards": [
            {"name": g.name, "stage": g.stage.value, "description": g.description, "config": g.config}
            for g in pipeline.output_guards
        ],
    }


class GuardCheckRequest(BaseModel):
    text: str
    stage: str = "input"     # "input" | "output"
    guard: str | None = None  # if set, return only that guard's outcome from the full stage run


@app.post("/internal/guardrails/check", dependencies=[Depends(_require_internal_token)])
async def check_guards(body: GuardCheckRequest) -> dict[str, Any]:
    """Run guardrails on arbitrary text without invoking the LLM.

    Use this to validate config changes, reproduce production blocks, or
    debug a specific guard in isolation. Since the pipeline is now a
    remote service, single-guard execution is implemented by running the
    full stage and filtering the per-guard outcomes client-side.
    """
    pipeline = app.state.guardrails
    stage = body.stage.lower()
    if stage not in ("input", "output"):
        raise HTTPException(400, "stage must be 'input' or 'output'")

    pr = await (pipeline.check_input(body.text) if stage == "input" else pipeline.check_output(body.text))

    if body.guard is not None:
        match = next((c for c in pr.checks if c.guard_name == body.guard), None)
        if match is None:
            active = [c.guard_name for c in pr.checks]
            raise HTTPException(
                404,
                f"guard {body.guard!r} did not run in stage {stage!r}. Active: {active}",
            )
        return {
            "stage": stage,
            "guard": match.guard_name,
            "decision": match.decision.value,
            "sanitized_text": match.sanitized_text,
            "reasons": match.reasons,
            "categories": match.categories,
            "score": match.score,
            "metadata": match.metadata,
        }

    return {
        "stage": stage,
        "allowed": pr.allowed,
        "sanitized_text": pr.sanitized_text,
        "duration_ms": round(pr.duration_ms, 2),
        "block_reasons": pr.block_reasons,
        "block_categories": pr.block_categories,
        "checks": [
            {
                "guard": c.guard_name,
                "decision": c.decision.value,
                "reasons": c.reasons,
                "categories": c.categories,
                "score": c.score,
                "metadata": c.metadata,
            }
            for c in pr.checks
        ],
    }
