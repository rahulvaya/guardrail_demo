"""azure-pii-detection: Azure AI Language PII Entity Recognition guardrail.

Uses the Azure AI Language ``:analyze-text`` endpoint with kind
``PiiEntityRecognition`` to detect PII categories that Azure AI Content
Safety does NOT cover (SSN, credit card, phone, email, address, etc.).

INPUT stage  -> sanitize (mask) by default; ``mode: "block"`` to reject.
OUTPUT stage -> sanitize the model reply before it leaves the agent.

Configuration (env: ``GUARD_AZURE_PII_DETECTION_CONFIG`` JSON):

    endpoint           Azure AI Language / Cognitive Services endpoint.
                       Defaults to env ``AZURE_LANGUAGE_ENDPOINT`` or, if
                       absent, ``AZURE_CONTENT_SAFETY_ENDPOINT`` (works
                       when both APIs share the same multi-service AI
                       Services / Cognitive Services resource).
    api_key            Subscription key. Defaults to env
                       ``AZURE_LANGUAGE_KEY`` or ``AZURE_CONTENT_SAFETY_KEY``.
                       When empty, AAD bearer auth is used (env
                       ``AZURE_LANGUAGE_AAD_TOKEN`` /
                       ``AZURE_CONTENT_SAFETY_AAD_TOKEN`` then
                       DefaultAzureCredential).
    api_version        Default ``2023-04-01``.
    language           Document language. Default ``en``.
    mode               ``sanitize`` (default) | ``block``.
    min_confidence     Drop entities below this score. Default 0.5.
    categories         Optional whitelist of PII categories to act on
                       (e.g. ``["USSocialSecurityNumber","CreditCardNumber"]``).
                       Empty = all returned categories.
    exclude_categories Optional blocklist of PII categories to ignore
                       (e.g. ``["PersonType"]`` to skip job-title noise).
                       Applied after ``categories`` whitelist.
    timeout_seconds    Default 5.
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
    LANGUAGE_API_VERSION,
    language_analyze_text_url,
)

log = logging.getLogger("agent.guardrails.azure_pii_detection")

DEFAULT_API_VERSION = LANGUAGE_API_VERSION
DEFAULT_MIN_CONFIDENCE = 0.5
_AAD_SCOPE = COGNITIVE_SERVICES_AAD_SCOPE


class AzurePiiDetectionGuard(Guard):
    name = "azure-pii-detection"
    stage = GuardStage.BOTH
    description = (
        "Azure AI Language PII Entity Recognition: detect SSN, credit card, "
        "email, phone, address, etc. in input/output and mask or block."
    )

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)
        self.endpoint: str = (
            config.get("endpoint")
            or os.getenv("AZURE_LANGUAGE_ENDPOINT")
            or os.getenv("AZURE_CONTENT_SAFETY_ENDPOINT", "")
        ).rstrip("/")
        self.api_key: str = (
            config.get("api_key")
            or os.getenv("AZURE_LANGUAGE_KEY")
            or os.getenv("AZURE_CONTENT_SAFETY_KEY", "")
        )
        self.api_version: str = config.get("api_version", DEFAULT_API_VERSION)
        self.language: str = str(config.get("language", "en"))
        self.mode: str = str(config.get("mode", "sanitize")).lower()
        self.min_confidence: float = float(
            config.get("min_confidence", DEFAULT_MIN_CONFIDENCE)
        )
        self.categories: list[str] = list(config.get("categories", []))
        self.exclude_categories: set[str] = {
            str(c) for c in config.get("exclude_categories", []) or []
        }
        self.timeout_seconds: float = float(config.get("timeout_seconds", 5.0))
        self.fail_open: bool = bool(config.get("fail_open", True))

        self._aad_credential: Any = None
        self._client: httpx.AsyncClient | None = None

        if not self.endpoint:
            log.warning(
                "azure-pii-detection: no endpoint configured "
                "(set AZURE_LANGUAGE_ENDPOINT); guard will fail-%s",
                "open" if self.fail_open else "closed",
            )

    # ------------------------------------------------------------------

    def _get_client(self) -> httpx.AsyncClient:
        from ..azure_http import get_client
        return get_client(timeout=self.timeout_seconds)

    async def aclose(self) -> None:
        return None

    def _auth_headers_sync(self) -> dict[str, str] | None:
        if self.api_key:
            return {"Ocp-Apim-Subscription-Key": self.api_key}
        token = (
            os.getenv("AZURE_LANGUAGE_AAD_TOKEN")
            or os.getenv("AZURE_CONTENT_SAFETY_AAD_TOKEN")
        )
        if token:
            return {"Authorization": f"Bearer {token}"}
        return None

    async def _auth_headers(self) -> dict[str, str]:
        fast = self._auth_headers_sync()
        if fast is not None:
            return fast
        try:
            from ..aad_cache import get_bearer_token
            token = await get_bearer_token(_AAD_SCOPE)
            if token:
                return {"Authorization": f"Bearer {token}"}
        except Exception as e:  # noqa: BLE001
            log.warning("azure-pii-detection: AAD auth unavailable: %r", e)
        return {}

    # ------------------------------------------------------------------

    async def check(self, text: str, *, context: dict[str, Any] | None = None) -> GuardCheckResult:
        if not text or not text.strip():
            return self._allow(text)

        if not self.endpoint:
            return self._fail(text, reason="no endpoint configured")

        headers = {"Content-Type": "application/json", **(await self._auth_headers())}
        if "Ocp-Apim-Subscription-Key" not in headers and "Authorization" not in headers:
            return self._fail(text, reason="no credentials available")

        url = language_analyze_text_url(self.endpoint, self.api_version)
        payload = {
            "kind": "PiiEntityRecognition",
            "parameters": {"modelVersion": "latest"},
            "analysisInput": {
                "documents": [
                    {"id": "1", "language": self.language, "text": text}
                ]
            },
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
            return self._fail(text, reason=repr(e))

        try:
            doc = body["results"]["documents"][0]
        except (KeyError, IndexError):
            return self._fail(text, reason=f"unexpected response shape: {body}")

        entities = doc.get("entities", []) or []
        # Filter by confidence and optional category whitelist
        kept: list[dict[str, Any]] = []
        for ent in entities:
            score = float(ent.get("confidenceScore", 0.0))
            cat = str(ent.get("category", ""))
            if score < self.min_confidence:
                continue
            if self.categories and cat not in self.categories:
                continue
            if cat in self.exclude_categories:
                continue
            kept.append(ent)

        # Per-category breakdown for the UI (always populated so users see
        # which categories were checked).
        by_cat: dict[str, int] = {}
        for ent in kept:
            cat = str(ent.get("category", "Unknown"))
            by_cat[cat] = by_cat.get(cat, 0) + 1

        # Show all categories the API returned at all confidences as info,
        # even when below threshold, so the UI reflects reality.
        all_cats: dict[str, float] = {}
        for ent in entities:
            cat = str(ent.get("category", "Unknown"))
            score = float(ent.get("confidenceScore", 0.0))
            all_cats[cat] = max(all_cats.get(cat, 0.0), score)

        category_results = []
        seen_cats = set(by_cat.keys()) | set(all_cats.keys())
        if not seen_cats:
            # No PII found at all -> show a single "PII" pill as passed
            category_results.append(
                {"category": "PII", "severity": 0, "passed": True}
            )
        else:
            for cat in sorted(seen_cats):
                count = by_cat.get(cat, 0)
                # severity_field reused as the count; passed=True when no
                # entity made it past the confidence filter for this cat.
                category_results.append(
                    {
                        "category": cat,
                        "severity": count if count > 0 else 0,
                        "passed": count == 0,
                    }
                )

        meta: dict[str, Any] = {
            "category_results": category_results,
            "min_confidence": self.min_confidence,
            "check": "language:PiiEntityRecognition",
        }

        if not kept:
            return self._allow(text, metadata=meta)

        # Sanitize using Azure's redactedText if available, else mask manually
        sanitized = doc.get("redactedText") or self._mask_locally(text, kept)
        reasons = [
            f"{ent['category']} (score={float(ent['confidenceScore']):.2f})"
            for ent in kept
        ]
        cats_out = [f"pii.{ent['category'].lower()}" for ent in kept]

        if self.mode == "block":
            return self._block(
                text, reasons=reasons, categories=cats_out,
                score=max(float(e["confidenceScore"]) for e in kept),
                metadata=meta,
            )
        return self._sanitize(
            sanitized, reasons=reasons, categories=cats_out,
            score=max(float(e["confidenceScore"]) for e in kept),
            metadata=meta,
        )

    # ------------------------------------------------------------------

    @staticmethod
    def _mask_locally(text: str, entities: list[dict[str, Any]]) -> str:
        out = text
        # Apply masks from rightmost to leftmost so offsets stay valid.
        for ent in sorted(entities, key=lambda e: int(e.get("offset", 0)), reverse=True):
            offset = int(ent.get("offset", 0))
            length = int(ent.get("length", 0))
            cat = str(ent.get("category", "PII")).upper()
            out = out[:offset] + f"<{cat}>" + out[offset + length:]
        return out

    def _fail(self, text: str, *, reason: str) -> GuardCheckResult:
        skipped = [
            {"category": c, "severity": None, "passed": None,
             "skipped": True, "reason": reason}
            for c in (self.categories or ["PII"])
        ]
        meta = {
            "error": reason,
            "fail_open": self.fail_open,
            "category_results": skipped,
            "check": "unavailable",
        }
        if self.fail_open:
            log.warning("azure-pii-detection fail-open: %s", reason)
            return self._allow(text, metadata=meta)
        log.warning("azure-pii-detection fail-closed: %s", reason)
        return self._block(
            text,
            reasons=[f"pii-detection unavailable: {reason}"],
            categories=["azure.unavailable"],
            metadata=meta,
        )


register_guard("azure-pii-detection", lambda cfg: AzurePiiDetectionGuard(**cfg))
