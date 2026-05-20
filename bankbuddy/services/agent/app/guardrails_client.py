"""Direct HTTP guardrails client used by the agent.

This is a self-contained, in-tree client - the agent talks to the
guardrails service over plain HTTP and does not depend on any external
SDK package. The shape it returns (PipelineResult / GuardCheckResult)
matches what the providers already consume.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx

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


class _TokenProvider:
    async def authorization_header(self) -> str:
        raise NotImplementedError


class _StaticTokenProvider(_TokenProvider):
    def __init__(self, token: str) -> None:
        self._token = token

    async def authorization_header(self) -> str:
        return f"Bearer {self._token}"


class _AadTokenProvider(_TokenProvider):
    def __init__(self, credential: Any, scope: str) -> None:
        if not scope:
            raise ValueError("aad_scope is required when using credential=")
        self._credential = credential
        self._scope = scope

    async def authorization_header(self) -> str:
        # azure-identity credential APIs are sync in practice.
        token = self._credential.get_token(self._scope)
        return f"Bearer {token.token}"


class _GuardStub:
    description: str = "(remote)"

    def __init__(self, name: str, stage: GuardStage) -> None:
        self.name = name
        self.stage = stage
        self.config: dict[str, Any] = {}


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

        if token is not None:
            self._provider: _TokenProvider = _StaticTokenProvider(token)
        else:
            self._provider = _AadTokenProvider(credential, aad_scope or "")

        self._policy_id = policy_id
        self._timeout = timeout_seconds
        self._block_message = block_message
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
        )
        self._policy_api_input_guards: list[str] = []
        self._policy_input_guards: list[str] = []
        self._policy_tool_input_guards: list[str] = []
        self._policy_output_guards: list[str] = []
        self._policy_tool_output_guards: list[str] = []
        self._policy_api_output_guards: list[str] = []

    @property
    def api_input_guards(self) -> list[Any]:
        return [_GuardStub(n, GuardStage.API_INPUT) for n in self._policy_api_input_guards]

    @property
    def input_guards(self) -> list[Any]:
        return [_GuardStub(n, GuardStage.INPUT) for n in self._policy_input_guards]

    @property
    def tool_input_guards(self) -> list[Any]:
        return [_GuardStub(n, GuardStage.TOOL_INPUT) for n in self._policy_tool_input_guards]

    @property
    def output_guards(self) -> list[Any]:
        return [_GuardStub(n, GuardStage.OUTPUT) for n in self._policy_output_guards]

    @property
    def tool_output_guards(self) -> list[Any]:
        return [_GuardStub(n, GuardStage.TOOL_OUTPUT) for n in self._policy_tool_output_guards]

    @property
    def api_output_guards(self) -> list[Any]:
        return [_GuardStub(n, GuardStage.API_OUTPUT) for n in self._policy_api_output_guards]

    async def warmup(self) -> None:
        try:
            headers = {"Authorization": await self._provider.authorization_header()}
            r = await self._client.get(f"/v1/policies/{self._policy_id}", headers=headers)
            r.raise_for_status()
            doc = r.json()
            self._policy_api_input_guards = [_name(item) for item in doc.get("api_input") or []]
            self._policy_input_guards = [_name(item) for item in doc.get("input") or []]
            self._policy_tool_input_guards = [_name(item) for item in doc.get("tool_input") or []]
            self._policy_output_guards = [_name(item) for item in doc.get("output") or []]
            self._policy_tool_output_guards = [_name(item) for item in doc.get("tool_output") or []]
            self._policy_api_output_guards = [_name(item) for item in doc.get("api_output") or []]
            log.info(
                "remote pipeline warmed up: policy=%s api_input=%s input=%s "
                "tool_input=%s output=%s tool_output=%s api_output=%s",
                self._policy_id,
                self._policy_api_input_guards,
                self._policy_input_guards,
                self._policy_tool_input_guards,
                self._policy_output_guards,
                self._policy_tool_output_guards,
                self._policy_api_output_guards,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("guardrails warmup failed (continuing): %s", e)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def check_api_input(
        self,
        text: str,
        *,
        context: dict[str, Any] | None = None,
        overrides: dict[str, dict[str, Any]] | None = None,
    ) -> PipelineResult:
        # No guards in the policy at this checkpoint -> skip the round trip.
        if not self._policy_api_input_guards:
            return _empty_result(text, GuardStage.API_INPUT)
        return await self._call("api_input", text, context, overrides, fail_closed=True)

    async def check_input(
        self,
        text: str,
        *,
        context: dict[str, Any] | None = None,
        overrides: dict[str, dict[str, Any]] | None = None,
    ) -> PipelineResult:
        return await self._call("input", text, context, overrides, fail_closed=True)

    async def check_tool_input(
        self,
        text: str,
        *,
        context: dict[str, Any] | None = None,
        overrides: dict[str, dict[str, Any]] | None = None,
    ) -> PipelineResult:
        if not self._policy_tool_input_guards:
            return _empty_result(text, GuardStage.TOOL_INPUT)
        return await self._call("tool_input", text, context, overrides, fail_closed=True)

    async def check_output(
        self,
        text: str,
        *,
        context: dict[str, Any] | None = None,
        overrides: dict[str, dict[str, Any]] | None = None,
    ) -> PipelineResult:
        return await self._call("output", text, context, overrides, fail_closed=False)

    async def check_tool_output(
        self,
        text: str,
        *,
        context: dict[str, Any] | None = None,
        overrides: dict[str, dict[str, Any]] | None = None,
    ) -> PipelineResult:
        if not self._policy_tool_output_guards:
            return _empty_result(text, GuardStage.TOOL_OUTPUT)
        return await self._call("tool_output", text, context, overrides, fail_closed=False)

    async def check_api_output(
        self,
        text: str,
        *,
        context: dict[str, Any] | None = None,
        overrides: dict[str, dict[str, Any]] | None = None,
    ) -> PipelineResult:
        if not self._policy_api_output_guards:
            return _empty_result(text, GuardStage.API_OUTPUT)
        return await self._call("api_output", text, context, overrides, fail_closed=False)

    async def _call(
        self,
        stage: str,
        text: str,
        context: dict[str, Any] | None,
        overrides: dict[str, dict[str, Any]] | None = None,
        *,
        fail_closed: bool,
    ) -> PipelineResult:
        body: dict[str, Any] = {
            "policy_id": self._policy_id,
            "stage": stage,
            "text": text,
            "context": context or {},
        }
        if overrides:
            body["overrides"] = overrides

        started = time.perf_counter()
        try:
            headers = {"Authorization": await self._provider.authorization_header()}
            r = await self._client.post("/v1/check", json=body, headers=headers)
            r.raise_for_status()
            payload = r.json()
        except (httpx.HTTPError, ValueError) as e:
            duration_ms = (time.perf_counter() - started) * 1000.0
            log.error(
                "guardrails service error stage=%s fail_mode=%s err=%r",
                stage,
                "closed" if fail_closed else "open",
                e,
            )
            return _synthetic_result(
                stage=stage,
                text=text,
                blocked=fail_closed,
                duration_ms=duration_ms,
                reason="guardrails-service-unreachable",
            )

        return _payload_to_pipeline_result(payload, stage)


def _empty_result(text: str, stage: GuardStage) -> PipelineResult:
    return PipelineResult(
        allowed=True,
        sanitized_text=text,
        stage=stage,
        checks=[],
        duration_ms=0.0,
    )


def _name(spec: Any) -> str:
    if isinstance(spec, str):
        return spec
    if isinstance(spec, dict) and len(spec) == 1:
        return next(iter(spec.keys()))
    return str(spec)


def _payload_to_pipeline_result(payload: dict[str, Any], stage: str) -> PipelineResult:
    checks = [
        GuardCheckResult(
            guard_name=g.get("name", "unknown"),
            decision=GuardDecision(g.get("decision", "allow")),
            sanitized_text=payload.get("sanitized_text", ""),
            reasons=g.get("reasons") or [],
            categories=g.get("categories") or [],
            score=g.get("score"),
            metadata=g.get("metadata") or {},
        )
        for g in payload.get("guards") or []
    ]
    decision = payload.get("decision", "allow")
    return PipelineResult(
        allowed=(decision != "block"),
        sanitized_text=payload.get("sanitized_text", ""),
        stage=GuardStage(stage),
        checks=checks,
        block_reasons=payload.get("block_reasons") or [],
        block_categories=payload.get("block_categories") or [],
        duration_ms=float(payload.get("duration_ms") or 0.0),
    )


def _synthetic_result(
    *, stage: str, text: str, blocked: bool, duration_ms: float, reason: str
) -> PipelineResult:
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
