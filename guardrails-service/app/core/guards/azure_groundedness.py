"""azure-groundedness: Azure AI Content Safety Groundedness Detection.

Calls the managed Azure groundedness API to verify that an assistant
reply (or any text) is supported by the supplied source documents.
This is OUTPUT-side hallucination protection that complements the
local heuristic ``groundedness`` guard.

This guard requires source material. It expects either:
  * ``context["sources"]``       - list[str], OR
  * ``context["grounding_sources"]``  - list[str]

If neither is present, the guard short-circuits and ALLOWS (it cannot
evaluate without sources). Set ``require_sources: true`` in config to
BLOCK instead.

Configuration (env: ``GUARD_AZURE_GROUNDEDNESS_CONFIG`` JSON):

    endpoint            Azure AI Content Safety endpoint.
                        Defaults to ``AZURE_CONTENT_SAFETY_ENDPOINT``.
    api_key             Subscription key. Defaults to
                        ``AZURE_CONTENT_SAFETY_KEY``. Falls back to
                        ``AZURE_CONTENT_SAFETY_AAD_TOKEN`` then
                        DefaultAzureCredential.
    api_version         Defaults to ``2024-09-01``.
    domain              ``Generic`` (default) or ``Medical``.
    task                ``Summarization`` or ``QnA``. Default ``QnA``.
    require_sources     If true, BLOCK when no sources are supplied.
                        Default false (ALLOW so the rest of the pipeline
                        can still run).
    timeout_seconds     Per-request timeout. Default 8.
    fail_open           On API errors, ALLOW (True, default) or BLOCK.

Docs: https://learn.microsoft.com/azure/ai-services/content-safety/quickstart-groundedness
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
    content_safety_groundedness_url,
)

log = logging.getLogger("agent.guardrails.azure_groundedness")


class AzureGroundednessGuard(Guard):
    name = "azure-groundedness"
    stage = GuardStage.OUTPUT
    description = (
        "Azure AI Content Safety managed Groundedness detection: verifies "
        "assistant replies are supported by supplied source documents."
    )

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)
        self.endpoint: str = (
            config.get("endpoint")
            or os.getenv("AZURE_CONTENT_SAFETY_ENDPOINT", "")
        ).rstrip("/")
        self.api_key: str = config.get("api_key") or os.getenv("AZURE_CONTENT_SAFETY_KEY", "")
        self.api_version: str = config.get("api_version", CONTENT_SAFETY_API_VERSION)
        self.domain: str = str(config.get("domain", "Generic"))
        self.task: str = str(config.get("task", "QnA"))
        self.require_sources: bool = bool(config.get("require_sources", False))
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
            log.warning("azure-groundedness: AAD auth unavailable: %r", e)
        return {}

    async def check(self, text: str, *, context: dict[str, Any] | None = None) -> GuardCheckResult:
        ctx = context or {}
        if not text or not text.strip():
            return self._allow(text)
        if not self.endpoint:
            return self._fail(text, "no endpoint configured")

        sources = ctx.get("sources") or ctx.get("grounding_sources") or []
        if isinstance(sources, str):
            sources = [sources]
        sources = [str(s) for s in sources if s]

        if not sources:
            if self.require_sources:
                return self._block(
                    text,
                    reasons=["azure-groundedness: no source documents provided"],
                    categories=["azure.groundedness.no_sources"],
                    metadata={"check": "text:detectGroundedness", "skipped": "no-sources"},
                )
            return self._allow(
                text,
                metadata={"check": "text:detectGroundedness", "skipped": "no-sources"},
            )

        # QnA mode requires a query (the original user question).
        query = str(ctx.get("query") or ctx.get("user_query") or "").strip()
        task = self.task
        if task == "QnA" and not query:
            task = "Summarization"

        headers = {"Content-Type": "application/json", **(await self._auth_headers())}
        if "Ocp-Apim-Subscription-Key" not in headers and "Authorization" not in headers:
            return self._fail(text, "no credentials available")

        payload: dict[str, Any] = {
            "domain": self.domain,
            "task": task,
            "text": text,
            "groundingSources": sources,
        }
        if task == "QnA":
            payload["qna"] = {"query": query}

        url = content_safety_groundedness_url(self.endpoint, self.api_version)
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

        ungrounded = bool(body.get("ungroundedDetected", False))
        ungrounded_percentage = float(body.get("ungroundedPercentage", 0.0))
        details = body.get("ungroundedDetails") or []
        meta = {
            "check": "text:detectGroundedness",
            "domain": self.domain,
            "task": task,
            "ungrounded_detected": ungrounded,
            "ungrounded_percentage": ungrounded_percentage,
            "ungrounded_details": details[:5],
            "category_results": [
                {
                    "category": "Groundedness",
                    "severity": 6 if ungrounded else 0,
                    "passed": not ungrounded,
                }
            ],
        }
        if ungrounded:
            return self._block(
                text,
                reasons=[
                    f"ungrounded content detected ({ungrounded_percentage:.0%} ungrounded)"
                ],
                categories=["azure.groundedness"],
                score=ungrounded_percentage,
                metadata=meta,
            )
        return self._allow(text, score=1.0 - ungrounded_percentage, metadata=meta)

    def _fail(self, text: str, reason: str) -> GuardCheckResult:
        meta = {
            "error": reason,
            "fail_open": self.fail_open,
            "check": "text:detectGroundedness",
            "category_results": [
                {"category": "Groundedness", "severity": None, "passed": None,
                 "skipped": True, "reason": reason}
            ],
        }
        if self.fail_open:
            log.warning("azure-groundedness fail-open: %s", reason)
            return self._allow(text, metadata=meta)
        log.warning("azure-groundedness fail-closed: %s", reason)
        return self._block(
            text,
            reasons=[f"azure-groundedness unavailable: {reason}"],
            categories=["azure.unavailable"],
            metadata=meta,
        )


register_guard("azure-groundedness", lambda cfg: AzureGroundednessGuard(**cfg))
