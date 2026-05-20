"""azure-topic-relevance: Azure AI Content Safety Custom Categories.

Managed counterpart to the local ``topic-relevance`` guard. Calls
Azure AI Content Safety ``text:analyze`` with one or more custom
category names that represent OFF-TOPIC content for your agent (e.g.
``non_banking``). If any of those categories fires above
``severity_threshold`` the user prompt is BLOCKED before it ever
reaches the LLM.

ONE-TIME SETUP (Azure portal)
-----------------------------
1. In your Content Safety resource, create a Standard Custom Category
   named e.g. ``non_banking``.
2. Upload positive samples (~50+ off-topic queries: "what is atlanta",
   "write a poem", "stock tips", "weather", ...).
3. Train + deploy the category. Note the exact category name.
4. List that name in this guard's ``categories:`` config.

Docs:
  https://learn.microsoft.com/azure/ai-services/content-safety/concepts/custom-categories

CONFIGURATION (policy YAML)
---------------------------
    azure-topic-relevance:
      enabled: true
      categories:                 # custom category names trained in ACS
        - non_banking
      severity_threshold: 2       # 0 safe, 2 low, 4 medium, 6 high
      blocklist_names: []         # optional ACS blocklists to OR with
      api_version: "2024-09-01"   # override per resource if needed
      url_path: "/contentsafety/text:analyze"  # rapid mode: override
      fail_open: true             # ALLOW on error (default) or BLOCK
      timeout_seconds: 5.0
"""
from __future__ import annotations

import asyncio
from typing import Any

from ..base import GuardCheckResult, GuardStage
from ..registry import register_guard
from ._azure_base import AzureGuardBase
from .azure_endpoints import (
    CONTENT_SAFETY_CUSTOM_CATEGORY_PATH,
    content_safety_custom_category_url,
)


class AzureTopicRelevanceGuard(AzureGuardBase):
    name = "azure-topic-relevance"
    stage = GuardStage.INPUT
    description = (
        "Azure AI Content Safety Custom Categories: BLOCK user prompts "
        "classified as off-topic for the agent's declared scope."
    )

    DEFAULT_API_VERSION = "2024-09-15-preview"
    CHECK_NAME = "text:analyzeCustomCategory"

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)
        # Path defaults to the canonical Custom Categories Standard
        # inference endpoint (see azure_endpoints.py). Override only
        # when targeting an inline-definition Rapid preview path.
        self.url_path: str = config.get(
            "url_path", CONTENT_SAFETY_CUSTOM_CATEGORY_PATH
        )
        self.categories: list[Any] = list(config.get("categories", []))
        # Single version applied to every configured category. Override
        # per-category by passing a dict in ``categories`` instead of a
        # plain list (see _category_pairs below).
        self.category_version: int = int(config.get("category_version", 1))
        self.blocklist_names: list[str] = list(config.get("blocklist_names", []))
        self.severity_threshold: int = int(config.get("severity_threshold", 2))
        self.refusal_message: str = str(
            config.get(
                "refusal_message",
                "I can only help with topics inside my declared scope.",
            )
        )

    def _url(self) -> str:
        return content_safety_custom_category_url(
            self.endpoint, self.api_version, self.url_path
        )

    def _category_pairs(self) -> list[tuple[str, int]]:
        """Normalise ``categories`` config into ``(name, version)`` tuples.

        Accepts either:
          categories: ["non_banking", "competitor_talk"]
        or:
          categories:
            - {name: non_banking, version: 2}
            - competitor_talk          # uses self.category_version
        """
        pairs: list[tuple[str, int]] = []
        for entry in self.categories:
            if isinstance(entry, dict):
                name = str(entry.get("name") or entry.get("categoryName") or "").strip()
                version = int(entry.get("version", self.category_version))
            else:
                name = str(entry).strip()
                version = self.category_version
            if name:
                pairs.append((name, version))
        return pairs

    async def check(self, text: str, *, context: dict[str, Any] | None = None) -> GuardCheckResult:
        if not text or not text.strip():
            return self._allow(text)
        pairs = self._category_pairs()
        if not pairs:
            return self._allow(
                text,
                metadata={
                    "check": self.CHECK_NAME,
                    "skipped": "no-categories-configured",
                },
            )

        short_circuit, headers = await self._prepare_request(text)
        if short_circuit is not None:
            return short_circuit

        url = self._url()

        async def _one(name: str, version: int) -> tuple[str, int, dict[str, Any] | None, str | None]:
            payload = {"text": text, "categoryName": name, "version": version}
            body, err = await self._post_json(url, payload, headers=headers)
            return name, version, body, err

        # Custom Categories Standard checks ONE category per call. Fan
        # out concurrently across configured categories and OR the
        # detections. Was sequential before; for N categories this turns
        # N round-trips into ~1.
        results = await asyncio.gather(*[_one(n, v) for n, v in pairs])

        cat_results: list[dict[str, Any]] = []
        offending: list[str] = []
        for name, version, body, err in results:
            if err is not None:
                return self._fail(text, f"category={name}: {err}")
            assert body is not None
            # Custom Categories Standard response:
            #   {"customCategoryAnalysis": {"detected": bool}}
            analysis = body.get("customCategoryAnalysis") or {}
            detected = bool(analysis.get("detected", False))
            cat_results.append(
                {
                    "category": f"azure.topic.{name}",
                    "version": version,
                    "detected": detected,
                    "passed": not detected,
                }
            )
            if detected:
                offending.append(f"{name} (v{version})")

        meta = {
            "check": self.CHECK_NAME,
            "category_results": cat_results,
        }

        if offending:
            return self._block(
                text,
                reasons=[
                    f"azure-topic-relevance: off-scope custom category fired ({'; '.join(offending)})",
                    self.refusal_message,
                ],
                categories=["azure.topic.off-scope"],
                score=1.0,
                metadata=meta,
            )
        return self._allow(text, score=0.0, metadata=meta)

    def _fail(self, text: str, reason: str) -> GuardCheckResult:
        return self._fail_result(
            text,
            reason=reason,
            extra_meta={"check": self.CHECK_NAME},
        )


register_guard("azure-topic-relevance", lambda cfg: AzureTopicRelevanceGuard(**cfg))
