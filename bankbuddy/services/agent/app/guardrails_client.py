"""HTTP client wrapper that makes the remote guardrails service look
like the local `GuardrailPipeline` to the rest of the agent.

The agent's `langgraph_provider` calls `pipeline.check_input(text, context=...)`
and `pipeline.check_output(text, context=...)` and reads `PipelineResult`.
This adapter speaks HTTP to the guardrails service and returns the same
shape, so providers stay unchanged.

Failure modes (locked):
    * INPUT  unreachable / 5xx / timeout -> fail-closed (BLOCK).
    * OUTPUT unreachable / 5xx / timeout -> fail-open (ALLOW + log).
The intent: never let a degraded guardrails service silently expose the
LLM to user input, and never let it hold a clean answer hostage.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx
from bankbuddy_shared.contracts.guardrails import (
    GuardrailsCheckRequest,
    GuardrailsCheckResponse,
)


# ---------------------------------------------------------------------------
# Minimal local mirrors of the guard pipeline value types.
#
# The canonical definitions live in the guardrails service
# (`services/guardrails/app/core/base.py` and `pipeline.py`). The agent
# only needs the dataclass shapes to expose to its own providers, so we
# duplicate those tiny types here rather than depend on the service code.
# ---------------------------------------------------------------------------


class GuardDecision(str, Enum):
    ALLOW = "allow"
    SANITIZE = "sanitize"
    BLOCK = "block"


class GuardStage(str, Enum):
    INPUT = "input"
    OUTPUT = "output"
    TOOL_OUTPUT = "tool_output"
    BOTH = "both"


@dataclass
class GuardCheckResult:
    guard_name: str
    decision: GuardDecision
    sanitized_text: str
    reasons: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    score: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return self.decision == GuardDecision.BLOCK


@dataclass
class PipelineResult:
    allowed: bool
    sanitized_text: str
    stage: GuardStage
    checks: list[GuardCheckResult] = field(default_factory=list)
    block_reasons: list[str] = field(default_factory=list)
    block_categories: list[str] = field(default_factory=list)
    duration_ms: float = 0.0

    @property
    def was_modified(self) -> bool:
        return any(c.decision == GuardDecision.SANITIZE for c in self.checks)


log = logging.getLogger("agent.guardrails.remote")


class RemoteGuardrailPipeline:
    """Drop-in replacement for `GuardrailPipeline` backed by HTTP."""

    # Stage names exposed for parity with the local pipeline's properties.
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        policy_id: str = "bankbuddy-default",
        timeout_seconds: float = 5.0,
        block_message: str = "I'm sorry - I can't help with that request.",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._policy_id = policy_id
        self._timeout = timeout_seconds
        self._block_message = block_message
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout_seconds,
        )
        self._policy_input_guards: list[str] = []
        self._policy_output_guards: list[str] = []
        self._policy_tool_output_guards: list[str] = []

    # ------------------------------------------------------------------
    # Parity with local GuardrailPipeline (read-only views)
    # ------------------------------------------------------------------
    @property
    def input_guards(self) -> list[Any]:
        return [_GuardStub(n, GuardStage.INPUT) for n in self._policy_input_guards]

    @property
    def output_guards(self) -> list[Any]:
        return [_GuardStub(n, GuardStage.OUTPUT) for n in self._policy_output_guards]

    @property
    def tool_output_guards(self) -> list[Any]:
        return [_GuardStub(n, GuardStage.TOOL_OUTPUT) for n in self._policy_tool_output_guards]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def warmup(self) -> None:
        """Best-effort policy fetch so input/output_guards are populated.

        Failure here does not prevent the agent from starting; the lists
        stay empty and admin endpoints just show no guards.
        """
        try:
            r = await self._client.get(f"/v1/policies/{self._policy_id}")
            r.raise_for_status()
            doc = r.json()
            self._policy_input_guards = [_name(item) for item in doc.get("input") or []]
            self._policy_output_guards = [_name(item) for item in doc.get("output") or []]
            self._policy_tool_output_guards = [_name(item) for item in doc.get("tool_output") or []]
            log.info(
                "remote pipeline warmed up: policy=%s input=%s output=%s tool_output=%s",
                self._policy_id,
                self._policy_input_guards,
                self._policy_output_guards,
                self._policy_tool_output_guards,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("guardrails warmup failed (continuing): %s", e)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Pipeline API
    # ------------------------------------------------------------------
    async def check_input(self, text: str, *, context: dict[str, Any] | None = None) -> PipelineResult:
        return await self._call("input", text, context, fail_closed=True)

    async def check_output(self, text: str, *, context: dict[str, Any] | None = None) -> PipelineResult:
        return await self._call("output", text, context, fail_closed=False)

    async def check_tool_output(
        self, text: str, *, context: dict[str, Any] | None = None
    ) -> PipelineResult:
        """Scan a tool's JSON result before it is fed back to the LLM.

        Skipped (returns a synthetic ALLOW result) when no tool_output guards
        are configured for the active policy, so we don't pay the HTTP
        round-trip per hop unnecessarily.

        Fail mode is OPEN: if the guardrails service is unreachable we let
        the tool result through and log it, mirroring the OUTPUT stage.
        Operators who need fail-closed should disable the affected tool at
        the agent level instead.
        """
        if not self._policy_tool_output_guards:
            # No guards configured for this stage. Skip the HTTP round-trip
            # and return an empty ALLOW result so traces stay quiet.
            return PipelineResult(
                allowed=True,
                sanitized_text=text,
                stage=GuardStage.TOOL_OUTPUT,
                checks=[],
                duration_ms=0.0,
            )
        return await self._call("tool_output", text, context, fail_closed=False)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    async def _call(
        self,
        stage: str,
        text: str,
        context: dict[str, Any] | None,
        *,
        fail_closed: bool,
    ) -> PipelineResult:
        body = GuardrailsCheckRequest(
            policy_id=self._policy_id,
            stage=stage,  # type: ignore[arg-type]
            text=text,
            context=context or {},
        ).model_dump()

        started = time.perf_counter()
        try:
            r = await self._client.post("/v1/check", json=body)
            r.raise_for_status()
            payload = GuardrailsCheckResponse(**r.json())
        except (httpx.HTTPError, ValueError) as e:
            duration_ms = (time.perf_counter() - started) * 1000.0
            log.error(
                "guardrails service error stage=%s fail_mode=%s err=%r",
                stage, "closed" if fail_closed else "open", e,
            )
            return _synthetic_result(
                stage=stage, text=text,
                blocked=fail_closed,
                duration_ms=duration_ms,
                reason="guardrails-service-unreachable",
            )

        return _payload_to_pipeline_result(payload, stage)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _GuardStub:
    """Minimal stand-in so introspection endpoints can list remote guards."""

    description: str = "(remote)"

    def __init__(self, name: str, stage: GuardStage) -> None:
        self.name = name
        self.stage = stage
        self.config: dict[str, Any] = {}


def _name(spec: Any) -> str:
    if isinstance(spec, str):
        return spec
    if isinstance(spec, dict) and len(spec) == 1:
        return next(iter(spec.keys()))
    return str(spec)


def _payload_to_pipeline_result(payload: GuardrailsCheckResponse, stage: str) -> PipelineResult:
    checks = [
        GuardCheckResult(
            guard_name=g.name,
            decision=GuardDecision(g.decision),
            sanitized_text=payload.sanitized_text,
            reasons=g.reasons,
            categories=g.categories,
            score=g.score,
            metadata=g.metadata,
        )
        for g in payload.guards
    ]
    return PipelineResult(
        allowed=(payload.decision != "block"),
        sanitized_text=payload.sanitized_text,
        stage=GuardStage(stage),
        checks=checks,
        block_reasons=payload.block_reasons,
        block_categories=payload.block_categories,
        duration_ms=payload.duration_ms,
    )


def _synthetic_result(
    *, stage: str, text: str, blocked: bool, duration_ms: float, reason: str,
) -> PipelineResult:
    """Build a PipelineResult locally when the service can't be reached."""
    decision = GuardDecision.BLOCK if blocked else GuardDecision.ALLOW
    check = GuardCheckResult(
        guard_name="guardrails-service",
        decision=decision,
        sanitized_text=text,
        reasons=[reason],
        categories=["service.unavailable"],
    )
    return PipelineResult(
        allowed=not blocked,
        sanitized_text=text,
        stage=GuardStage(stage),
        checks=[check],
        block_reasons=[reason] if blocked else [],
        block_categories=["service.unavailable"] if blocked else [],
        duration_ms=duration_ms,
    )
