"""azure-task-adherence: Azure AI Content Safety Task Adherence detection.

Calls the managed Azure task-adherence API to verify that an assistant
reply stays within the agent's declared task / scope. This is the
managed counterpart to the local ``task-adherence`` guard.

The guard reads the agent's task definition from context. Accepted
keys (first non-empty wins):
  * ``context["task_definition"]``  - str
  * ``context["system_prompt"]``    - str

Without a task definition the guard short-circuits and ALLOWS (it
cannot evaluate). Set ``require_task_definition: true`` to BLOCK.

Configuration (env: ``GUARD_AZURE_TASK_ADHERENCE_CONFIG`` JSON):

    endpoint                    Azure AI Content Safety endpoint.
    api_key                     Subscription key. Falls back to AAD.
    api_version                 Defaults to ``2024-09-01``.
    require_task_definition     BLOCK when no task definition present.
                                Default false.
    timeout_seconds             Per-request timeout. Default 8.
    fail_open                   On API errors, ALLOW (default) or BLOCK.

Docs: https://learn.microsoft.com/azure/ai-services/content-safety/concepts/task-adherence
"""
from __future__ import annotations

from typing import Any

from ..base import GuardCheckResult, GuardStage
from ..registry import register_guard
from ._azure_base import AzureGuardBase
from .azure_endpoints import (
    CONTENT_SAFETY_API_VERSION,
    content_safety_task_adherence_url,
)


class AzureTaskAdherenceGuard(AzureGuardBase):
    name = "azure-task-adherence"
    stage = GuardStage.OUTPUT
    description = (
        "Azure AI Content Safety managed Task Adherence: blocks replies "
        "that drift outside the agent's declared task / scope."
    )

    DEFAULT_API_VERSION = CONTENT_SAFETY_API_VERSION
    CHECK_NAME = "text:detectTaskAdherence"

    def __init__(self, **config: Any) -> None:
        config.setdefault("timeout_seconds", 8.0)
        super().__init__(**config)
        self.require_task_definition: bool = bool(
            config.get("require_task_definition", False)
        )
        # Optional static task definition supplied via policy config. Used
        # as a fallback when the per-call context does not carry one, so
        # policy authors can pin a fixed agent scope (e.g. "banking
        # assistant") without changing the calling code.
        self.task_definition: str = str(config.get("task_definition", "")).strip()

    async def check(self, text: str, *, context: dict[str, Any] | None = None) -> GuardCheckResult:
        ctx = context or {}
        if not text or not text.strip():
            return self._allow(text)

        task_def = str(
            ctx.get("task_definition")
            or ctx.get("system_prompt")
            or self.task_definition
            or ""
        ).strip()
        if not task_def:
            if self.require_task_definition:
                return self._block(
                    text,
                    reasons=["azure-task-adherence: no task definition provided"],
                    categories=["azure.task_adherence.no_task"],
                    metadata={"check": self.CHECK_NAME, "skipped": "no-task"},
                )
            return self._allow(
                text,
                metadata={"check": self.CHECK_NAME, "skipped": "no-task"},
            )

        short_circuit, headers = await self._prepare_request(text)
        if short_circuit is not None:
            return short_circuit

        payload: dict[str, Any] = {
            "taskDefinition": task_def,
            "agentResponse": text,
        }
        # Optional context for higher precision.
        if ctx.get("query") or ctx.get("user_query"):
            payload["userQuery"] = str(ctx.get("query") or ctx.get("user_query"))
        if ctx.get("tool_definitions"):
            payload["toolDefinitions"] = ctx["tool_definitions"]

        url = content_safety_task_adherence_url(self.endpoint, self.api_version)
        body, err = await self._post_json(url, payload, headers=headers)
        if err is not None:
            return self._fail(text, err)

        assert body is not None
        adherence = body.get("taskAdherence") or {}
        adheres = bool(adherence.get("adheres", True))
        reason = str(adherence.get("reason", ""))
        score = float(adherence.get("score", 1.0 if adheres else 0.0))
        meta = {
            "check": self.CHECK_NAME,
            "adheres": adheres,
            "reason": reason,
            "score": score,
            "category_results": [
                {
                    "category": "TaskAdherence",
                    "severity": 0 if adheres else 6,
                    "passed": adheres,
                }
            ],
        }
        if not adheres:
            return self._block(
                text,
                reasons=[
                    f"task-adherence: response drifted from declared task ({reason or 'no reason'})"
                ],
                categories=["azure.task_adherence"],
                score=score,
                metadata=meta,
            )
        return self._allow(text, score=score, metadata=meta)

    def _fail(self, text: str, reason: str) -> GuardCheckResult:
        return self._fail_result(
            text,
            reason=reason,
            skipped_categories=[self._skipped_pill("TaskAdherence", reason)],
            extra_meta={"check": self.CHECK_NAME},
        )


register_guard("azure-task-adherence", lambda cfg: AzureTaskAdherenceGuard(**cfg))
