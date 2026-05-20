"""Pipeline composing multiple `Guard` instances."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from .base import Guard, GuardCheckResult, GuardDecision, GuardStage

log = logging.getLogger("agent.guardrails")

# Run guards within a stage in parallel by default. Set
# GUARDRAILS_SEQUENTIAL=1 to fall back to the legacy sequential behaviour
# (useful when a custom guard depends on seeing a prior guard's
# sanitized_text mid-stage).
_PARALLEL = os.getenv("GUARDRAILS_SEQUENTIAL", "0") not in ("1", "true", "True", "yes")


@dataclass
class PipelineResult:
    """Aggregated result of running a pipeline stage."""

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


class GuardrailPipeline:
    """Run a list of guards in declared order.

    Semantics per guard:
        ALLOW    -> continue to next guard with current text
        SANITIZE -> replace text with `sanitized_text`, continue
        BLOCK    -> stop immediately, mark pipeline blocked

    The pipeline records every guard's result (including those that didn't run
    because of an earlier block) so traces are complete.
    """

    def __init__(
        self,
        input_guards: list[Guard],
        output_guards: list[Guard],
        tool_output_guards: list[Guard] | None = None,
        *,
        api_input_guards: list[Guard] | None = None,
        tool_input_guards: list[Guard] | None = None,
        api_output_guards: list[Guard] | None = None,
    ) -> None:
        self._api_input_guards = api_input_guards or []
        self._input_guards = input_guards
        self._tool_input_guards = tool_input_guards or []
        self._output_guards = output_guards
        self._tool_output_guards = tool_output_guards or []
        self._api_output_guards = api_output_guards or []

    @property
    def api_input_guards(self) -> list[Guard]:
        return list(self._api_input_guards)

    @property
    def input_guards(self) -> list[Guard]:
        return list(self._input_guards)

    @property
    def tool_input_guards(self) -> list[Guard]:
        return list(self._tool_input_guards)

    @property
    def output_guards(self) -> list[Guard]:
        return list(self._output_guards)

    @property
    def tool_output_guards(self) -> list[Guard]:
        return list(self._tool_output_guards)

    @property
    def api_output_guards(self) -> list[Guard]:
        return list(self._api_output_guards)

    async def check_api_input(self, text: str, *, context: dict[str, Any] | None = None) -> PipelineResult:
        return await self._run(self._api_input_guards, text, GuardStage.API_INPUT, context)

    async def check_input(self, text: str, *, context: dict[str, Any] | None = None) -> PipelineResult:
        return await self._run(self._input_guards, text, GuardStage.INPUT, context)

    async def check_tool_input(self, text: str, *, context: dict[str, Any] | None = None) -> PipelineResult:
        return await self._run(self._tool_input_guards, text, GuardStage.TOOL_INPUT, context)

    async def check_output(self, text: str, *, context: dict[str, Any] | None = None) -> PipelineResult:
        return await self._run(self._output_guards, text, GuardStage.OUTPUT, context)

    async def check_tool_output(
        self, text: str, *, context: dict[str, Any] | None = None
    ) -> PipelineResult:
        return await self._run(self._tool_output_guards, text, GuardStage.TOOL_OUTPUT, context)

    async def check_api_output(self, text: str, *, context: dict[str, Any] | None = None) -> PipelineResult:
        return await self._run(self._api_output_guards, text, GuardStage.API_OUTPUT, context)

    async def _run(
        self,
        guards: list[Guard],
        text: str,
        stage: GuardStage,
        context: dict[str, Any] | None,
    ) -> PipelineResult:
        started = time.perf_counter()
        current = text
        checks: list[GuardCheckResult] = []
        blocked = False
        block_reasons: list[str] = []
        block_categories: list[str] = []

        # Make the current stage available to guards (e.g. azure-content-safety
        # only calls Prompt Shields on input).
        guard_context = dict(context or {})
        guard_context.setdefault("stage", stage.value)

        if not guards:
            return PipelineResult(
                allowed=True,
                sanitized_text=current,
                stage=stage,
                checks=[],
                duration_ms=(time.perf_counter() - started) * 1000.0,
            )

        if _PARALLEL and len(guards) > 1:
            # Fan-out: run every guard against the ORIGINAL text concurrently.
            # All guards see the same input, so a guard cannot observe an
            # earlier guard's sanitization mid-stage. Sanitizes are still
            # applied to the final returned text in declaration order, and
            # the trace records every guard's decision (including ones that
            # came in after a peer blocked). This is safe because in-tree
            # guards are independent classifiers; if you have a guard that
            # truly depends on a peer's sanitized output, set
            # GUARDRAILS_SEQUENTIAL=1.
            async def _safe(g: Guard) -> GuardCheckResult:
                try:
                    return await g.check(text, context=guard_context)
                except Exception as e:  # noqa: BLE001
                    log.exception("guard %s crashed; treating as ALLOW", g.name)
                    return GuardCheckResult(
                        guard_name=g.name,
                        decision=GuardDecision.ALLOW,
                        sanitized_text=text,
                        reasons=[f"guard-error: {e!r}"],
                    )

            results = await asyncio.gather(*[_safe(g) for g in guards])

            # Walk results in declaration order so traces and the
            # "first block wins" semantic are preserved.
            for guard, result in zip(guards, results):
                checks.append(result)
                if result.decision == GuardDecision.BLOCK:
                    if not blocked:
                        blocked = True
                        block_reasons.extend(result.reasons or [f"blocked by {guard.name}"])
                        block_categories.extend(result.categories)
                    log.warning(
                        "guardrail BLOCK stage=%s guard=%s reasons=%s",
                        stage.value, guard.name, result.reasons,
                    )
                elif result.decision == GuardDecision.SANITIZE and not blocked:
                    log.info(
                        "guardrail SANITIZE stage=%s guard=%s reasons=%s",
                        stage.value, guard.name, result.reasons,
                    )
                    current = result.sanitized_text
        else:
            for guard in guards:
                try:
                    result = await guard.check(current, context=guard_context)
                except Exception as e:  # noqa: BLE001 - guards must never crash the pipeline
                    log.exception("guard %s crashed; treating as ALLOW", guard.name)
                    result = GuardCheckResult(
                        guard_name=guard.name,
                        decision=GuardDecision.ALLOW,
                        sanitized_text=current,
                        reasons=[f"guard-error: {e!r}"],
                    )
                checks.append(result)

                if result.decision == GuardDecision.BLOCK:
                    blocked = True
                    block_reasons.extend(result.reasons or [f"blocked by {guard.name}"])
                    block_categories.extend(result.categories)
                    log.warning("guardrail BLOCK stage=%s guard=%s reasons=%s",
                                stage.value, guard.name, result.reasons)
                    break
                if result.decision == GuardDecision.SANITIZE:
                    log.info("guardrail SANITIZE stage=%s guard=%s reasons=%s",
                             stage.value, guard.name, result.reasons)
                    current = result.sanitized_text

        return PipelineResult(
            allowed=not blocked,
            sanitized_text=current,
            stage=stage,
            checks=checks,
            block_reasons=block_reasons,
            block_categories=block_categories,
            duration_ms=(time.perf_counter() - started) * 1000.0,
        )

    async def aclose(self) -> None:
        for g in [
            *self._api_input_guards,
            *self._input_guards,
            *self._tool_input_guards,
            *self._output_guards,
            *self._tool_output_guards,
            *self._api_output_guards,
        ]:
            try:
                await g.aclose()
            except Exception:  # noqa: BLE001
                log.debug("error closing guard %s", g.name, exc_info=True)
