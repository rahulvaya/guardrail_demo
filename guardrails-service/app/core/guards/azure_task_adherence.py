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

import logging
import os
from typing import Any

import httpx

from ..base import Guard, GuardCheckResult, GuardStage
from ..registry import register_guard
from .azure_endpoints import (
    COGNITIVE_SERVICES_AAD_SCOPE,
    CONTENT_SAFETY_API_VERSION,
    content_safety_task_adherence_url,
)

log = logging.getLogger("agent.guardrails.azure_task_adherence")


class AzureTaskAdherenceGuard(Guard):
    name = "azure-task-adherence"
    stage = GuardStage.OUTPUT
    description = (
        "Azure AI Content Safety managed Task Adherence: blocks replies "
        "that drift outside the agent's declared task / scope."
    )

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)
        self.endpoint: str = (
            config.get("endpoint")
            or os.getenv("AZURE_CONTENT_SAFETY_ENDPOINT", "")
        ).rstrip("/")
        self.api_key: str = config.get("api_key") or os.getenv("AZURE_CONTENT_SAFETY_KEY", "")
        self.api_version: str = config.get("api_version", CONTENT_SAFETY_API_VERSION)
        self.require_task_definition: bool = bool(
            config.get("require_task_definition", False)
        )
        # Optional static task definition supplied via policy config. Used
        # as a fallback when the per-call context does not carry one, so
        # policy authors can pin a fixed agent scope (e.g. "banking
        # assistant") without changing the calling code.
        self.task_definition: str = str(config.get("task_definition", "")).strip()
        self.timeout_seconds: float = float(config.get("timeout_seconds", 8.0))
        self.fail_open: bool = bool(config.get("fail_open", True))

        self._aad_token_env = "AZURE_CONTENT_SAFETY_AAD_TOKEN"
        self._aad_credential: Any = None
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        from ..azure_http import get_client
        return get_client(timeout=self.timeout_seconds)

    async def aclose(self) -> None:
        return None

    def _auth_headers_sync(self) -> dict[str, str] | None:
        if self.api_key:
            return {"Ocp-Apim-Subscription-Key": self.api_key}
        token = os.getenv(self._aad_token_env)
        if token:
            return {"Authorization": f"Bearer {token}"}
        return None

    async def _auth_headers(self) -> dict[str, str]:
        fast = self._auth_headers_sync()
        if fast is not None:
            return fast
        try:
            from ..aad_cache import get_bearer_token
            token = await get_bearer_token(COGNITIVE_SERVICES_AAD_SCOPE)
            if token:
                return {"Authorization": f"Bearer {token}"}
        except Exception as e:  # noqa: BLE001
            log.warning("azure-task-adherence: AAD auth unavailable: %r", e)
        return {}

    async def check(self, text: str, *, context: dict[str, Any] | None = None) -> GuardCheckResult:
        ctx = context or {}
        if not text or not text.strip():
            return self._allow(text)
        if not self.endpoint:
            return self._fail(text, "no endpoint configured")

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
                    metadata={"check": "text:detectTaskAdherence", "skipped": "no-task"},
                )
            return self._allow(
                text,
                metadata={"check": "text:detectTaskAdherence", "skipped": "no-task"},
            )

        headers = {"Content-Type": "application/json", **(await self._auth_headers())}
        if "Ocp-Apim-Subscription-Key" not in headers and "Authorization" not in headers:
            return self._fail(text, "no credentials available")

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
        try:
            resp = await self._get_client().post(url, json=payload, headers=headers)
            resp.raise_for_status()
            body = resp.json()
        except httpx.HTTPStatusError as e:
            return self._fail(
                text, f"HTTP {e.response.status_code}: {e.response.text[:200]}"
            )
        except Exception as e:  # noqa: BLE001
            return self._fail(text, f"request error: {e!r}")

        adherence = body.get("taskAdherence") or {}
        adheres = bool(adherence.get("adheres", True))
        reason = str(adherence.get("reason", ""))
        score = float(adherence.get("score", 1.0 if adheres else 0.0))
        meta = {
            "check": "text:detectTaskAdherence",
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
        meta = {
            "error": reason,
            "fail_open": self.fail_open,
            "check": "text:detectTaskAdherence",
            "category_results": [
                {"category": "TaskAdherence", "severity": None, "passed": None,
                 "skipped": True, "reason": reason}
            ],
        }
        if self.fail_open:
            log.warning("azure-task-adherence fail-open: %s", reason)
            return self._allow(text, metadata=meta)
        log.warning("azure-task-adherence fail-closed: %s", reason)
        return self._block(
            text,
            reasons=[f"azure-task-adherence unavailable: {reason}"],
            categories=["azure.unavailable"],
            metadata=meta,
        )


register_guard("azure-task-adherence", lambda cfg: AzureTaskAdherenceGuard(**cfg))
