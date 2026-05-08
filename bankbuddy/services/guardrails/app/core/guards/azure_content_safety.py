"""azure-content-safety: Azure AI Content Safety guardrail.

This single guard wraps multiple Azure AI Content Safety APIs so one
managed service covers the bulk of input + output moderation:

INPUT stage:
  * ``text:shieldPrompt``  -> jailbreak / prompt-injection detection
  * ``text:analyze``       -> Hate / SelfHarm / Sexual / Violence

OUTPUT stage:
  * ``text:analyze``       -> Hate / SelfHarm / Sexual / Violence
                              (covers toxicity / harmful generation)

Coverage notes (read carefully):
  * Azure AI Content Safety does NOT natively cover regex-style PII
    (SSNs / cards), secret leakage (API keys), or banned-substring
    blocklists. Those concerns map to the local guards
    ``pii-detect`` / ``output-pii-redact`` / ``secret-leak`` /
    ``banned-substrings``. Re-enable them via their
    ``GUARD_*_ENABLED=true`` flags when you need that coverage.
  * Azure AI Language has a separate PII detection API; integrate it
    by extending ``pii_detect`` with an ``engine="azure-language"``
    branch (not enabled in this build).

Configuration (env: ``GUARD_AZURE_CONTENT_SAFETY_CONFIG`` JSON):

    endpoint           Azure AI Content Safety resource endpoint.
                       Defaults to env ``AZURE_CONTENT_SAFETY_ENDPOINT``.
    api_key            Subscription key. Defaults to env
                       ``AZURE_CONTENT_SAFETY_KEY``. When empty, the
                       guard falls back to Entra ID (a pre-fetched
                       ``AZURE_CONTENT_SAFETY_AAD_TOKEN`` or
                       ``DefaultAzureCredential``).
    api_version        Defaults to ``2024-09-01``.
    categories         Harm categories evaluated by ``text:analyze``.
                       Default: ``["Hate","SelfHarm","Sexual","Violence"]``.
    severity_threshold Block when any harm severity >= this value.
                       FourSeverityLevels: 0/2/4/6. Default 4.
    enable_prompt_shield  Run ``text:shieldPrompt`` on INPUT. Default True.
    timeout_seconds    Per-request timeout. Default 5.
    fail_open          On API errors, ALLOW (True, default) or BLOCK.
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
    content_safety_shield_prompt_url,
    content_safety_text_analyze_url,
)

log = logging.getLogger("agent.guardrails.azure_content_safety")

DEFAULT_CATEGORIES = ["Hate", "SelfHarm", "Sexual", "Violence"]
DEFAULT_API_VERSION = CONTENT_SAFETY_API_VERSION
DEFAULT_SEVERITY_THRESHOLD = 4   # 0=safe, 2=low, 4=medium, 6=high
_AAD_SCOPE = COGNITIVE_SERVICES_AAD_SCOPE


class AzureContentSafetyGuard(Guard):
    name = "azure-content-safety"
    stage = GuardStage.BOTH
    description = (
        "Azure AI Content Safety: harm categories (Hate/SelfHarm/Sexual/Violence) "
        "plus Prompt Shields jailbreak/prompt-injection detection on input."
    )

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)
        self.endpoint: str = (
            config.get("endpoint")
            or os.getenv("AZURE_CONTENT_SAFETY_ENDPOINT", "")
        ).rstrip("/")
        self.api_key: str = config.get("api_key") or os.getenv("AZURE_CONTENT_SAFETY_KEY", "")
        self.api_version: str = config.get("api_version", DEFAULT_API_VERSION)
        self.categories: list[str] = list(config.get("categories", DEFAULT_CATEGORIES))
        self.severity_threshold: int = int(
            config.get("severity_threshold", DEFAULT_SEVERITY_THRESHOLD)
        )
        self.enable_prompt_shield: bool = bool(config.get("enable_prompt_shield", True))
        self.timeout_seconds: float = float(config.get("timeout_seconds", 5.0))
        self.fail_open: bool = bool(config.get("fail_open", True))

        self._aad_token_env = "AZURE_CONTENT_SAFETY_AAD_TOKEN"
        self._aad_credential: Any = None
        self._client: httpx.AsyncClient | None = None

        if not self.endpoint:
            log.warning(
                "azure-content-safety: no endpoint configured "
                "(set AZURE_CONTENT_SAFETY_ENDPOINT); guard will fail-%s",
                "open" if self.fail_open else "closed",
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_seconds)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        if self.api_key:
            return {"Ocp-Apim-Subscription-Key": self.api_key}

        token = os.getenv(self._aad_token_env)
        if token:
            return {"Authorization": f"Bearer {token}"}

        try:  # pragma: no cover - best-effort import
            if self._aad_credential is None:
                from azure.identity import DefaultAzureCredential  # type: ignore
                self._aad_credential = DefaultAzureCredential()
            access = self._aad_credential.get_token(_AAD_SCOPE)
            return {"Authorization": f"Bearer {access.token}"}
        except Exception as e:  # noqa: BLE001
            log.warning("azure-content-safety: AAD auth unavailable: %r", e)
            return {}

    # ------------------------------------------------------------------
    # Check
    # ------------------------------------------------------------------

    async def check(self, text: str, *, context: dict[str, Any] | None = None) -> GuardCheckResult:
        if not text or not text.strip():
            return self._allow(text)

        if not self.endpoint:
            return self._fail(text, reason="no endpoint configured")

        headers = {"Content-Type": "application/json", **self._auth_headers()}
        if "Ocp-Apim-Subscription-Key" not in headers and "Authorization" not in headers:
            return self._fail(text, reason="no credentials available")

        stage = str((context or {}).get("stage", "input")).lower()

        # 1) INPUT-only: Prompt Shields (jailbreak / prompt-injection)
        prompt_shield_meta: dict[str, Any] | None = None
        if stage == "input" and self.enable_prompt_shield:
            shield_block, prompt_shield_meta = await self._run_prompt_shield(text, headers)
            if shield_block is not None:
                return shield_block

        # 2) Both stages: harm categories via text:analyze
        return await self._run_text_analyze(text, headers, prompt_shield_meta)

    # ------------------------------------------------------------------
    # Prompt Shields
    # ------------------------------------------------------------------

    async def _run_prompt_shield(
        self, text: str, headers: dict[str, str]
    ) -> tuple[GuardCheckResult | None, dict[str, Any] | None]:
        url = content_safety_shield_prompt_url(self.endpoint, self.api_version)
        payload = {"userPrompt": text, "documents": []}
        try:
            resp = await self._get_client().post(url, json=payload, headers=headers)
            resp.raise_for_status()
            body = resp.json()
        except httpx.HTTPStatusError as e:
            log.warning(
                "azure-content-safety prompt-shield HTTP %s: %s",
                e.response.status_code, e.response.text[:200],
            )
            return None, {"available": False, "error": f"HTTP {e.response.status_code}"}
        except Exception as e:  # noqa: BLE001
            log.warning("azure-content-safety prompt-shield error: %r", e)
            return None, {"available": False, "error": repr(e)}

        user_analysis = body.get("userPromptAnalysis") or {}
        attack = bool(user_analysis.get("attackDetected", False))
        meta = {
            "available": True,
            "attack_detected": attack,
            "user_prompt_analysis": user_analysis,
        }
        if attack:
            return (
                self._block(
                    text,
                    reasons=["prompt-shield: jailbreak / prompt-injection detected"],
                    categories=["azure.prompt_injection"],
                    score=1.0,
                    metadata={
                        "prompt_shield": meta,
                        "category_results": [
                            {"category": "PromptShield", "severity": 6, "passed": False},
                        ],
                        "check": "text:shieldPrompt",
                    },
                ),
                meta,
            )
        return None, meta

    # ------------------------------------------------------------------
    # text:analyze (harm categories)
    # ------------------------------------------------------------------

    async def _run_text_analyze(
        self,
        text: str,
        headers: dict[str, str],
        prompt_shield_meta: dict[str, Any] | None = None,
    ) -> GuardCheckResult:
        url = content_safety_text_analyze_url(self.endpoint, self.api_version)
        payload = {
            "text": text,
            "categories": self.categories,
            "outputType": "FourSeverityLevels",
        }
        try:
            resp = await self._get_client().post(url, json=payload, headers=headers)
            resp.raise_for_status()
            body = resp.json()
        except httpx.HTTPStatusError as e:
            return self._fail(
                text,
                reason=f"HTTP {e.response.status_code}: {e.response.text[:200]}",
            )
        except Exception as e:  # noqa: BLE001
            return self._fail(text, reason=f"request error: {e!r}")

        analyses = body.get("categoriesAnalysis") or []
        worst: dict[str, int] = {}
        triggered: list[tuple[str, int]] = []
        for entry in analyses:
            cat = str(entry.get("category", ""))
            sev = int(entry.get("severity", 0))
            worst[cat] = sev
            if sev >= self.severity_threshold:
                triggered.append((cat, sev))

        max_sev = max(worst.values(), default=0)
        # Per-category pass/fail breakdown so the UI can show every
        # evaluated category (not just the ones that triggered).
        category_results = [
            {
                "category": cat,
                "severity": worst.get(cat, 0),
                "passed": worst.get(cat, 0) < self.severity_threshold,
            }
            for cat in self.categories
        ]
        # If prompt-shield ran on this stage and passed, surface it as a
        # passed category alongside the harm categories.
        if prompt_shield_meta and prompt_shield_meta.get("available"):
            category_results.insert(
                0,
                {
                    "category": "PromptShield",
                    "severity": 6 if prompt_shield_meta.get("attack_detected") else 0,
                    "passed": not prompt_shield_meta.get("attack_detected"),
                },
            )

        common_meta: dict[str, Any] = {
            "severities": worst,
            "threshold": self.severity_threshold,
            "category_results": category_results,
            "check": "text:analyze",
        }
        if prompt_shield_meta is not None:
            common_meta["prompt_shield"] = prompt_shield_meta

        if triggered:
            triggered.sort(key=lambda x: -x[1])
            return self._block(
                text,
                reasons=[
                    f"{cat} severity {sev} >= {self.severity_threshold}"
                    for cat, sev in triggered
                ],
                categories=[f"azure.{cat.lower()}" for cat, _ in triggered],
                score=float(max_sev) / 6.0,
                metadata=common_meta,
            )

        return self._allow(text, score=float(max_sev) / 6.0, metadata=common_meta)

    # ------------------------------------------------------------------

    def _fail(self, text: str, *, reason: str) -> GuardCheckResult:
        # Surface the full list of checks the guard *would* have run so the
        # UI can render them as "skipped / not configured" pills.
        skipped_categories: list[dict[str, Any]] = []
        if self.enable_prompt_shield:
            skipped_categories.append(
                {"category": "PromptShield", "severity": None, "passed": None,
                 "skipped": True, "reason": reason}
            )
        for cat in self.categories:
            skipped_categories.append(
                {"category": cat, "severity": None, "passed": None,
                 "skipped": True, "reason": reason}
            )
        meta: dict[str, Any] = {
            "error": reason,
            "fail_open": self.fail_open,
            "category_results": skipped_categories,
            "threshold": self.severity_threshold,
            "check": "unavailable",
        }
        if self.fail_open:
            log.warning("azure-content-safety fail-open: %s", reason)
            return self._allow(text, metadata=meta)
        log.warning("azure-content-safety fail-closed: %s", reason)
        return self._block(
            text,
            reasons=[f"content-safety unavailable: {reason}"],
            categories=["azure.unavailable"],
            metadata=meta,
        )


register_guard("azure-content-safety", lambda cfg: AzureContentSafetyGuard(**cfg))
