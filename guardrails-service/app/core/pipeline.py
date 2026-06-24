"""Pipeline composing multiple `Guard` instances."""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any

from .base import Guard, GuardCheckResult, GuardDecision, GuardStage
from .observability import (
    obs_log,
    record_guard,
    record_guard_error,
    record_stage,
    safe_reasons,
    set_request_context,
)

# Run guards within a stage in parallel by default. Set
# GUARDRAILS_SEQUENTIAL=1 to fall back to the legacy sequential behaviour
# (useful when a custom guard depends on seeing a prior guard's
# sanitized_text mid-stage).
_PARALLEL = os.getenv("GUARDRAILS_SEQUENTIAL", "0") not in ("1", "true", "True", "yes")

# When a guard in a parallel stage returns BLOCK, cancel the other in-flight
# guards in that stage instead of waiting for them to finish. The trace still
# records every guard, but cancelled ones are reported with a synthetic
# "cancelled" reason. Default ON; disable with GUARDRAILS_CANCEL_ON_BLOCK=0
# to get the legacy "wait for all peers" behaviour.
_CANCEL_ON_BLOCK = os.getenv("GUARDRAILS_CANCEL_ON_BLOCK", "1") not in ("0", "false", "False", "no")


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
        # Auto-extract tool_name from {"tool": "...", "arguments": {...}} so guards
        # (schema-enforcement, azure-task-adherence) can read context["tool_name"]
        # without re-parsing the text themselves.
        import json as _json  # local import to avoid top-level circular risk
        ctx: dict[str, Any] = dict(context or {})
        if "tool_name" not in ctx:
            try:
                payload = _json.loads(text)
                if isinstance(payload, dict) and isinstance(payload.get("tool"), str):
                    ctx["tool_name"] = payload["tool"]
            except Exception:
                pass
        return await self._run(self._tool_input_guards, text, GuardStage.TOOL_INPUT, ctx)

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
        # Publish the stage on the observability contextvar so every
        # log/metric emitted by guards is tagged with it automatically.
        set_request_context(stage=stage.value)
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
            duration_s = time.perf_counter() - started
            record_stage(decision="allow", duration_seconds=duration_s)
            return PipelineResult(
                allowed=True,
                sanitized_text=current,
                stage=stage,
                checks=[],
                duration_ms=duration_s * 1000.0,
            )

        async def _timed(g: Guard) -> tuple[GuardCheckResult, float]:
            """Run one guard, recording timing + metrics. Never raises."""
            t0 = time.perf_counter()
            try:
                r = await g.check(text, context=guard_context)
            except Exception as e:  # noqa: BLE001
                dur = time.perf_counter() - t0
                obs_log(
                    "guard.crashed",
                    level="error",
                    guard=g.name,
                    error_type=type(e).__name__,
                    duration_ms=dur * 1000.0,
                    exc_info=True,
                )
                record_guard_error(guard=g.name)
                record_guard(
                    guard=g.name, decision="error", duration_seconds=dur,
                )
                return (
                    GuardCheckResult(
                        guard_name=g.name,
                        decision=GuardDecision.ALLOW,
                        sanitized_text=text,
                        # NOTE: error repr could in theory contain a slice of
                        # user text. Keep it out of `reasons` to be safe.
                        reasons=[f"guard-error: {type(e).__name__}"],
                    ),
                    dur,
                )
            dur = time.perf_counter() - t0
            record_guard(
                guard=g.name,
                decision=r.decision.value,
                duration_seconds=dur,
                categories=list(r.categories or []),
            )
            return (r, dur)

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
            tasks: list[asyncio.Task[tuple[GuardCheckResult, float]]] = [
                asyncio.create_task(_timed(g)) for g in guards
            ]
            results: list[tuple[GuardCheckResult, float] | None] = [None] * len(guards)
            task_to_idx = {t: i for i, t in enumerate(tasks)}

            if _CANCEL_ON_BLOCK:
                # Wait for results as they complete. On the first BLOCK,
                # cancel the rest and synthesize "cancelled" results for them
                # so the trace still lists every guard in the stage.
                first_block_seen = False
                for fut in asyncio.as_completed(tasks):
                    try:
                        result_tuple = await fut
                    except asyncio.CancelledError:
                        # Happens when we already started cancelling peers.
                        continue
                    done_task = next(
                        (t for t in tasks if t.done() and results[task_to_idx[t]] is None),
                        None,
                    )
                    if done_task is None:
                        continue
                    results[task_to_idx[done_task]] = result_tuple
                    if (
                        result_tuple[0].decision == GuardDecision.BLOCK
                        and not first_block_seen
                    ):
                        first_block_seen = True
                        for t in tasks:
                            if not t.done():
                                t.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        break
                # Fill in synthetic results for any guard whose task got
                # cancelled (or never completed for some other reason).
                for i, g in enumerate(guards):
                    if results[i] is None:
                        results[i] = (
                            GuardCheckResult(
                                guard_name=g.name,
                                decision=GuardDecision.ALLOW,
                                sanitized_text=text,
                                reasons=["cancelled: peer guard blocked first"],
                                metadata={"cancelled": True},
                            ),
                            0.0,
                        )
                resolved: list[tuple[GuardCheckResult, float]] = [
                    r for r in results if r is not None
                ]
            else:
                resolved = await asyncio.gather(*tasks)

            # Walk results in declaration order so traces and the
            # "first block wins" semantic are preserved.
            for guard, (result, dur) in zip(guards, resolved):
                checks.append(result)
                if result.decision == GuardDecision.BLOCK:
                    if not blocked:
                        blocked = True
                        # Full reasons go to the API caller via the
                        # PipelineResult; they are NEVER logged because
                        # ``banned-substrings`` puts the matched user text
                        # in `reasons`.
                        block_reasons.extend(result.reasons or [f"blocked by {guard.name}"])
                        block_categories.extend(result.categories)
                        # Use the guard's sanitized_text as the working text
                        # so redact_and_block mode surfaces masked PII in the
                        # response (for plain block mode this equals original).
                        current = result.sanitized_text
                    obs_log(
                        "guard.block",
                        level="warning",
                        guard=guard.name,
                        categories=list(result.categories or []),
                        reasons_redacted=safe_reasons(result.reasons),
                        duration_ms=dur * 1000.0,
                    )
                elif result.decision == GuardDecision.SANITIZE and not blocked:
                    obs_log(
                        "guard.sanitize",
                        level="info",
                        guard=guard.name,
                        categories=list(result.categories or []),
                        reasons_redacted=safe_reasons(result.reasons),
                        duration_ms=dur * 1000.0,
                    )
                    current = result.sanitized_text
        else:
            for guard in guards:
                result, dur = await _timed(guard)
                checks.append(result)
                if result.decision == GuardDecision.BLOCK:
                    blocked = True
                    block_reasons.extend(result.reasons or [f"blocked by {guard.name}"])
                    block_categories.extend(result.categories)
                    # Use the guard's sanitized_text so redact_and_block
                    # mode surfaces masked PII (plain block returns original).
                    current = result.sanitized_text
                    obs_log(
                        "guard.block",
                        level="warning",
                        guard=guard.name,
                        categories=list(result.categories or []),
                        reasons_redacted=safe_reasons(result.reasons),
                        duration_ms=dur * 1000.0,
                    )
                    break
                if result.decision == GuardDecision.SANITIZE:
                    obs_log(
                        "guard.sanitize",
                        level="info",
                        guard=guard.name,
                        categories=list(result.categories or []),
                        reasons_redacted=safe_reasons(result.reasons),
                        duration_ms=dur * 1000.0,
                    )
                    current = result.sanitized_text

        duration_s = time.perf_counter() - started
        decision_label = "block" if blocked else (
            "sanitize"
            if any(c.decision == GuardDecision.SANITIZE for c in checks)
            else "allow"
        )
        record_stage(decision=decision_label, duration_seconds=duration_s)

        return PipelineResult(
            allowed=not blocked,
            sanitized_text=current,
            stage=stage,
            checks=checks,
            block_reasons=block_reasons,
            block_categories=block_categories,
            duration_ms=duration_s * 1000.0,
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
                obs_log("guard.close_error", level="debug", guard=g.name, exc_info=True)
