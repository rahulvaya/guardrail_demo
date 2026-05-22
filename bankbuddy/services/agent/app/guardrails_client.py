"""Direct HTTP guardrails client used by the agent.

Talks to the guardrails service over plain HTTP and returns Pydantic
models (`PipelineResult` / `GuardCheckResult`) shaped for the providers.
"""
from __future__ import annotations

import logging
import time
from enum import Enum
from typing import Any, Awaitable, Callable

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

log = logging.getLogger("agent.guardrails_client")


class GuardDecision(str, Enum):
    ALLOW = "allow"
    SANITIZE = "sanitize"
    BLOCK = "block"


class GuardStage(str, Enum):
    API_INPUT = "api_input"
    INPUT = "input"
    TOOL_INPUT = "tool_input"
    OUTPUT = "output"
    TOOL_OUTPUT = "tool_output"
    API_OUTPUT = "api_output"
    BOTH = "both"


# Stages where an empty policy means we can skip the HTTP round trip.
_SKIP_IF_EMPTY = {
    GuardStage.API_INPUT,
    GuardStage.TOOL_INPUT,
    GuardStage.TOOL_OUTPUT,
    GuardStage.API_OUTPUT,
}


class GuardCheckResult(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    guard_name: str = Field(alias="name", default="unknown")
    decision: GuardDecision = GuardDecision.ALLOW
    reasons: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    score: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("reasons", "categories", mode="before")
    @classmethod
    def _none_to_list(cls, v: Any) -> Any:
        return v or []

    @field_validator("metadata", mode="before")
    @classmethod
    def _none_to_dict(cls, v: Any) -> Any:
        return v or {}

    @property
    def blocked(self) -> bool:
        return self.decision == GuardDecision.BLOCK


class PipelineResult(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    stage: GuardStage
    decision: GuardDecision = GuardDecision.ALLOW
    sanitized_text: str = ""
    checks: list[GuardCheckResult] = Field(default_factory=list, alias="guards")
    block_reasons: list[str] = Field(default_factory=list)
    block_categories: list[str] = Field(default_factory=list)
    duration_ms: float = 0.0

    @field_validator("checks", "block_reasons", "block_categories", mode="before")
    @classmethod
    def _none_to_list(cls, v: Any) -> Any:
        return v or []

    @field_validator("duration_ms", mode="before")
    @classmethod
    def _none_to_zero(cls, v: Any) -> Any:
        return v or 0.0

    @property
    def allowed(self) -> bool:
        return self.decision != GuardDecision.BLOCK

    @property
    def was_modified(self) -> bool:
        return any(c.decision == GuardDecision.SANITIZE for c in self.checks)


class GuardRef(BaseModel):
    """Lightweight stub for `*_guards` introspection properties."""
    name: str
    stage: GuardStage
    description: str = "(remote)"
    config: dict[str, Any] = Field(default_factory=dict)


_AuthHeader = Callable[[], Awaitable[str]]


class RemoteGuardrailPipeline:
    """Direct HTTP client with parity to the old guardrails SDK wrapper."""

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        credential: Any | None = None,
        aad_scope: str | None = None,
        policy_id: str = "bankbuddy-default",
        timeout_seconds: float = 5.0,
        block_message: str = "I'm sorry - I can't help with that request.",
    ) -> None:
        if (token is None) == (credential is None):
            raise ValueError("provide exactly one of token= or credential=")
        if credential is not None and not aad_scope:
            raise ValueError("aad_scope is required when using credential=")

        async def _auth_static() -> str:
            return f"Bearer {token}"

        async def _auth_aad() -> str:
            # azure-identity credential APIs are sync in practice.
            return f"Bearer {credential.get_token(aad_scope).token}"  # type: ignore[arg-type]

        self._auth: _AuthHeader = _auth_static if token is not None else _auth_aad
        self._policy_id = policy_id
        self._block_message = block_message
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_seconds)
        self._policy_guards: dict[GuardStage, list[str]] = {s: [] for s in GuardStage}

    # ------------------------------------------------------------- introspection
    def _stubs(self, stage: GuardStage) -> list[GuardRef]:
        return [GuardRef(name=n, stage=stage) for n in self._policy_guards[stage]]

    @property
    def api_input_guards(self) -> list[GuardRef]: return self._stubs(GuardStage.API_INPUT)
    @property
    def input_guards(self) -> list[GuardRef]: return self._stubs(GuardStage.INPUT)
    @property
    def tool_input_guards(self) -> list[GuardRef]: return self._stubs(GuardStage.TOOL_INPUT)
    @property
    def output_guards(self) -> list[GuardRef]: return self._stubs(GuardStage.OUTPUT)
    @property
    def tool_output_guards(self) -> list[GuardRef]: return self._stubs(GuardStage.TOOL_OUTPUT)
    @property
    def api_output_guards(self) -> list[GuardRef]: return self._stubs(GuardStage.API_OUTPUT)

    # ----------------------------------------------------------------- lifecycle
    async def warmup(self) -> None:
        try:
            r = await self._client.get(
                f"/v1/policies/{self._policy_id}",
                headers={"Authorization": await self._auth()},
            )
            r.raise_for_status()
            doc = r.json()
            for stage in GuardStage:
                self._policy_guards[stage] = [_name(i) for i in (doc.get(stage.value) or [])]
            log.info(
                "remote pipeline warmed up: policy=%s guards=%s",
                self._policy_id,
                {s.value: ns for s, ns in self._policy_guards.items() if ns},
            )
        except Exception as e:  # noqa: BLE001
            log.warning("guardrails warmup failed (continuing): %s", e)

    async def aclose(self) -> None:
        await self._client.aclose()

    # -------------------------------------------------------------------- checks
    async def check_api_input(self, text: str, *, context=None, overrides=None) -> PipelineResult:
        return await self._call(GuardStage.API_INPUT, text, context, overrides, fail_closed=True)

    async def check_input(self, text: str, *, context=None, overrides=None) -> PipelineResult:
        return await self._call(GuardStage.INPUT, text, context, overrides, fail_closed=True)

    async def check_tool_input(self, text: str, *, context=None, overrides=None) -> PipelineResult:
        return await self._call(GuardStage.TOOL_INPUT, text, context, overrides, fail_closed=True)

    async def check_output(self, text: str, *, context=None, overrides=None) -> PipelineResult:
        return await self._call(GuardStage.OUTPUT, text, context, overrides, fail_closed=False)

    async def check_tool_output(self, text: str, *, context=None, overrides=None) -> PipelineResult:
        return await self._call(GuardStage.TOOL_OUTPUT, text, context, overrides, fail_closed=False)

    async def check_api_output(self, text: str, *, context=None, overrides=None) -> PipelineResult:
        return await self._call(GuardStage.API_OUTPUT, text, context, overrides, fail_closed=False)

    async def _call(
        self,
        stage: GuardStage,
        text: str,
        context: dict[str, Any] | None,
        overrides: dict[str, dict[str, Any]] | None,
        *,
        fail_closed: bool,
    ) -> PipelineResult:
        if stage in _SKIP_IF_EMPTY and not self._policy_guards[stage]:
            return PipelineResult(stage=stage, sanitized_text=text)

        body: dict[str, Any] = {
            "policy_id": self._policy_id,
            "stage": stage.value,
            "text": text,
            "context": context or {},
        }
        if overrides:
            body["overrides"] = overrides

        started = time.perf_counter()
        try:
            r = await self._client.post(
                "/v1/check",
                json=body,
                headers={"Authorization": await self._auth()},
            )
            r.raise_for_status()
            return PipelineResult.model_validate({**r.json(), "stage": stage.value})
        except (httpx.HTTPError, ValueError) as e:
            log.error(
                "guardrails service error stage=%s fail_mode=%s err=%r",
                stage.value, "closed" if fail_closed else "open", e,
            )
            decision = GuardDecision.BLOCK if fail_closed else GuardDecision.ALLOW
            reason = "guardrails-service-unreachable"
            return PipelineResult(
                stage=stage,
                decision=decision,
                sanitized_text=text,
                checks=[GuardCheckResult(
                    guard_name="guardrails-service",
                    decision=decision,
                    reasons=[reason],
                    categories=["service.unavailable"],
                )],
                block_reasons=[reason] if fail_closed else [],
                block_categories=["service.unavailable"] if fail_closed else [],
                duration_ms=(time.perf_counter() - started) * 1000.0,
            )


def _name(spec: Any) -> str:
    if isinstance(spec, str):
        return spec
    if isinstance(spec, dict) and len(spec) == 1:
        return next(iter(spec))
    return str(spec)
