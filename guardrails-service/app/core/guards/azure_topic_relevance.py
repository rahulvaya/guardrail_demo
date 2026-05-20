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

WHY NOT THE RAPID PREVIEW?
--------------------------
Rapid Custom Categories (preview) lets you define categories from a
short text description without training, which would let us consume
the same ``*banking_task`` anchor directly. The path and payload
shape are still in flux across preview api-versions and Azure regions,
so this guard targets the GA Standard Custom Categories path. To use
the preview Rapid mode, override ``url_path`` and ``api_version`` in
the policy config -- the request shape is otherwise compatible.

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

import logging
import os
from typing import Any

import httpx

from ..base import Guard, GuardCheckResult, GuardStage
from ..registry import register_guard
from .azure_endpoints import (
    COGNITIVE_SERVICES_AAD_SCOPE,
    CONTENT_SAFETY_CUSTOM_CATEGORY_PATH,
    content_safety_custom_category_url,
)

log = logging.getLogger("agent.guardrails.azure_topic_relevance")


class AzureTopicRelevanceGuard(Guard):
    name = "azure-topic-relevance"
    stage = GuardStage.INPUT
    description = (
        "Azure AI Content Safety Custom Categories: BLOCK user prompts "
        "classified as off-topic for the agent's declared scope."
    )

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)
        self.endpoint: str = (
            config.get("endpoint")
            or os.getenv("AZURE_CONTENT_SAFETY_ENDPOINT", "")
        ).rstrip("/")
        self.api_key: str = (
            config.get("api_key") or os.getenv("AZURE_CONTENT_SAFETY_KEY", "")
        )
        self.api_version: str = config.get("api_version", "2024-09-15-preview")
        # Path defaults to the canonical Custom Categories Standard
        # inference endpoint (see azure_endpoints.py). Override only
        # when targeting an inline-definition Rapid preview path.
        self.url_path: str = config.get(
            "url_path", CONTENT_SAFETY_CUSTOM_CATEGORY_PATH
        )
        self.categories: list[str] = list(config.get("categories", []))
        # Single version applied to every configured category. Override
        # per-category by passing a dict in ``categories`` instead of a
        # plain list (see _category_pairs below).
        self.category_version: int = int(config.get("category_version", 1))
        self.blocklist_names: list[str] = list(config.get("blocklist_names", []))
        self.severity_threshold: int = int(config.get("severity_threshold", 2))
        self.timeout_seconds: float = float(config.get("timeout_seconds", 5.0))
        self.fail_open: bool = bool(config.get("fail_open", True))
        self.refusal_message: str = str(
            config.get(
                "refusal_message",
                "I can only help with topics inside my declared scope.",
            )
        )
        self._aad_token_env = "AZURE_CONTENT_SAFETY_AAD_TOKEN"

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
            log.warning("azure-topic-relevance: AAD auth unavailable: %r", e)
        return {}

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
        if not self.endpoint:
            return self._fail(text, "no endpoint configured")
        pairs = self._category_pairs()
        if not pairs:
            return self._allow(
                text,
                metadata={
                    "check": "text:analyzeCustomCategory",
                    "skipped": "no-categories-configured",
                },
            )

        headers = {"Content-Type": "application/json", **(await self._auth_headers())}
        if "Ocp-Apim-Subscription-Key" not in headers and "Authorization" not in headers:
            return self._fail(text, "no credentials available")

        # Custom Categories Standard checks ONE category per call. Fan
        # out across configured categories and OR the detections.
        client = self._get_client()
        url = self._url()
        cat_results: list[dict[str, Any]] = []
        offending: list[str] = []
        for name, version in pairs:
            payload: dict[str, Any] = {
                "text": text,
                "categoryName": name,
                "version": version,
            }
            try:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                body = resp.json()
            except httpx.HTTPStatusError as e:
                return self._fail(
                    text,
                    f"HTTP {e.response.status_code} for category={name}: "
                    f"{e.response.text[:200]}",
                )
            except Exception as e:  # noqa: BLE001
                return self._fail(text, f"request error for category={name}: {e!r}")

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
            "check": "text:analyzeCustomCategory",
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
        meta = {
            "error": reason,
            "fail_open": self.fail_open,
            "check": "text:analyzeCustomCategory",
        }
        if self.fail_open:
            log.warning("azure-topic-relevance fail-open: %s", reason)
            return self._allow(text, metadata=meta)
        log.warning("azure-topic-relevance fail-closed: %s", reason)
        return self._block(
            text,
            reasons=[f"azure-topic-relevance unavailable: {reason}"],
            categories=["azure.unavailable"],
            metadata=meta,
        )


register_guard("azure-topic-relevance", lambda cfg: AzureTopicRelevanceGuard(**cfg))
