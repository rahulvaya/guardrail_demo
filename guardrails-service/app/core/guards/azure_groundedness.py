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

from typing import Any

from ..base import GuardCheckResult, GuardStage
from ..registry import register_guard
from ._azure_base import AzureGuardBase
from .azure_endpoints import (
    CONTENT_SAFETY_API_VERSION,
    content_safety_groundedness_url,
)


class AzureGroundednessGuard(AzureGuardBase):
    name = "azure-groundedness"
    stage = GuardStage.OUTPUT
    description = (
        "Azure AI Content Safety managed Groundedness detection: verifies "
        "assistant replies are supported by supplied source documents."
    )

    DEFAULT_API_VERSION = CONTENT_SAFETY_API_VERSION
    CHECK_NAME = "text:detectGroundedness"

    def __init__(self, **config: Any) -> None:
        # Groundedness historically used a longer 8s default; keep it.
        config.setdefault("timeout_seconds", 8.0)
        super().__init__(**config)
        self.domain: str = str(config.get("domain", "Generic"))
        self.task: str = str(config.get("task", "QnA"))
        self.require_sources: bool = bool(config.get("require_sources", False))

    async def check(self, text: str, *, context: dict[str, Any] | None = None) -> GuardCheckResult:
        ctx = context or {}
        if not text or not text.strip():
            return self._allow(text)

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
                    metadata={"check": self.CHECK_NAME, "skipped": "no-sources"},
                )
            return self._allow(
                text,
                metadata={"check": self.CHECK_NAME, "skipped": "no-sources"},
            )

        short_circuit, headers = await self._prepare_request(text)
        if short_circuit is not None:
            return short_circuit

        # QnA mode requires a query (the original user question).
        query = str(ctx.get("query") or ctx.get("user_query") or "").strip()
        task = self.task
        if task == "QnA" and not query:
            task = "Summarization"

        payload: dict[str, Any] = {
            "domain": self.domain,
            "task": task,
            "text": text,
            "groundingSources": sources,
        }
        if task == "QnA":
            payload["qna"] = {"query": query}

        url = content_safety_groundedness_url(self.endpoint, self.api_version)
        body, err = await self._post_json(url, payload, headers=headers)
        if err is not None:
            return self._fail(text, err)

        assert body is not None
        ungrounded = bool(body.get("ungroundedDetected", False))
        ungrounded_percentage = float(body.get("ungroundedPercentage", 0.0))
        details = body.get("ungroundedDetails") or []
        meta = {
            "check": self.CHECK_NAME,
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
        return self._fail_result(
            text,
            reason=reason,
            skipped_categories=[self._skipped_pill("Groundedness", reason)],
            extra_meta={"check": self.CHECK_NAME},
        )


register_guard("azure-groundedness", lambda cfg: AzureGroundednessGuard(**cfg))
