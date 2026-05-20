"""azure-content-safety: Azure AI Content Safety guardrail.

This single guard wraps multiple Azure AI Content Safety APIs so one
managed service covers most input + output moderation needs:

INPUT stage:
  * ``text:shieldPrompt``  -> jailbreak / prompt-injection detection
  * ``text:analyze``       -> Hate / SelfHarm / Sexual / Violence
                              (+ optional Text Blocklists)

OUTPUT stage:
  * ``text:analyze``       -> Hate / SelfHarm / Sexual / Violence
                              (+ optional Text Blocklists)
  * ``text:detectProtectedMaterial`` -> copyrighted text detection

Sibling Azure guards (separate modules):
  * ``azure-groundedness``   -> {endpoint}/text:detectGroundedness
  * ``azure-task-adherence`` -> {endpoint}/text:detectTaskAdherence

Coverage notes:
  * Azure AI Content Safety does NOT natively cover regex-style PII
    (SSNs / cards) or secret leakage (API keys). Those map to the local
    guards ``pii-detect`` / ``secret-leak``.
  * Azure AI Language has a separate PII detection API covered by the
    ``azure-pii-detection`` guard.

Configuration (env: ``GUARD_AZURE_CONTENT_SAFETY_CONFIG`` JSON):

    endpoint                Azure AI Content Safety resource endpoint.
                            Defaults to env ``AZURE_CONTENT_SAFETY_ENDPOINT``.
    api_key                 Subscription key. Defaults to env
                            ``AZURE_CONTENT_SAFETY_KEY``. When empty, the
                            guard falls back to Entra ID.
    api_version             Defaults to ``2024-09-01``.
    categories              Harm categories evaluated by ``text:analyze``.
                            Default: ``["Hate","SelfHarm","Sexual","Violence"]``.
    severity_threshold      Block when any harm severity >= this value.
                            FourSeverityLevels: 0/2/4/6. Default 4.
    enable_prompt_shield    Run ``text:shieldPrompt`` on INPUT. Default True.
    enable_protected_material  Run ``text:detectProtectedMaterial`` on
                            OUTPUT. Default False (opt-in).
    blocklist_names         List of Azure-managed blocklist names to
                            apply in ``text:analyze``. Default [].
    halt_on_blocklist_hit   Block immediately on blocklist match.
                            Default True.
    timeout_seconds         Per-request timeout. Default 5.
    fail_open               On API errors, ALLOW (True, default) or BLOCK.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..base import GuardCheckResult, GuardStage
from ..registry import register_guard
from ._azure_base import AzureGuardBase
from .azure_endpoints import (
    CONTENT_SAFETY_API_VERSION,
    content_safety_protected_material_url,
    content_safety_shield_prompt_url,
    content_safety_text_analyze_url,
)

log = logging.getLogger("agent.guardrails.azure_content_safety")

DEFAULT_CATEGORIES = ["Hate", "SelfHarm", "Sexual", "Violence"]
DEFAULT_SEVERITY_THRESHOLD = 4   # 0=safe, 2=low, 4=medium, 6=high


class AzureContentSafetyGuard(AzureGuardBase):
    name = "azure-content-safety"
    stage = GuardStage.BOTH
    description = (
        "Azure AI Content Safety: harm categories (Hate/SelfHarm/Sexual/Violence) "
        "plus Prompt Shields jailbreak/prompt-injection detection on input."
    )

    DEFAULT_API_VERSION = CONTENT_SAFETY_API_VERSION
    CHECK_NAME = "text:analyze"

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)
        self.categories: list[str] = list(config.get("categories", DEFAULT_CATEGORIES))
        self.severity_threshold: int = int(
            config.get("severity_threshold", DEFAULT_SEVERITY_THRESHOLD)
        )
        self.enable_prompt_shield: bool = bool(config.get("enable_prompt_shield", True))
        self.enable_protected_material: bool = bool(
            config.get("enable_protected_material", False)
        )
        self.blocklist_names: list[str] = list(config.get("blocklist_names", []))
        self.halt_on_blocklist_hit: bool = bool(
            config.get("halt_on_blocklist_hit", True)
        )

    # ------------------------------------------------------------------
    # Check
    # ------------------------------------------------------------------

    async def check(self, text: str, *, context: dict[str, Any] | None = None) -> GuardCheckResult:
        short_circuit, headers = await self._prepare_request(text)
        if short_circuit is not None:
            return short_circuit

        stage = str((context or {}).get("stage", "input")).lower()
        # Input-family stages (api_input, input/llm_input, tool_input) get
        # Prompt Shields. Output-family stages (output/llm_output, tool_output,
        # api_output) get Protected Material. Harm categories run on both.
        input_family = stage in ("api_input", "input", "llm_input", "tool_input")

        # Run the optional pre-check (Prompt Shield on input-family, Protected
        # Material on output-family) IN PARALLEL with text:analyze, since they
        # are independent Azure API calls. Whichever first decision says BLOCK
        # wins; otherwise we return the text:analyze result. Cuts wall-clock
        # latency of this guard from ~2 round-trips to ~1.
        analyze_task = asyncio.create_task(self._run_text_analyze(text, headers, None))

        pre_task: asyncio.Task | None = None
        if input_family and self.enable_prompt_shield:
            async def _shield_only() -> GuardCheckResult | None:
                shield_block, _meta = await self._run_prompt_shield(text, headers)
                return shield_block
            pre_task = asyncio.create_task(_shield_only())
        elif (not input_family) and self.enable_protected_material:
            pre_task = asyncio.create_task(self._run_protected_material(text, headers))

        if pre_task is not None:
            pre_block = await pre_task
            if pre_block is not None:
                analyze_task.cancel()
                try:
                    await analyze_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                return pre_block

        return await analyze_task

    # ------------------------------------------------------------------
    # Prompt Shields
    # ------------------------------------------------------------------

    async def _run_prompt_shield(
        self, text: str, headers: dict[str, str]
    ) -> tuple[GuardCheckResult | None, dict[str, Any] | None]:
        url = content_safety_shield_prompt_url(self.endpoint, self.api_version)
        payload = {"userPrompt": text, "documents": []}
        body, err = await self._post_json(url, payload, headers=headers)
        if err is not None:
            log.warning("azure-content-safety prompt-shield error: %s", err)
            return None, {"available": False, "error": err}

        assert body is not None
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
        payload: dict[str, Any] = {
            "text": text,
            "categories": self.categories,
            "outputType": "FourSeverityLevels",
        }
        if self.blocklist_names:
            payload["blocklistNames"] = self.blocklist_names
            payload["haltOnBlocklistHit"] = self.halt_on_blocklist_hit

        body, err = await self._post_json(url, payload, headers=headers)
        if err is not None:
            return self._fail(text, reason=err)

        assert body is not None
        analyses = body.get("categoriesAnalysis") or []
        worst: dict[str, int] = {}
        triggered: list[tuple[str, int]] = []
        for entry in analyses:
            cat = str(entry.get("category", ""))
            sev = int(entry.get("severity", 0))
            worst[cat] = sev
            if sev >= self.severity_threshold:
                triggered.append((cat, sev))

        # Azure Text Blocklists: any match is a hard block (irrespective
        # of severity_threshold).
        blocklist_matches = body.get("blocklistsMatch") or []

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
            "check": self.CHECK_NAME,
        }
        if prompt_shield_meta is not None:
            common_meta["prompt_shield"] = prompt_shield_meta
        if self.blocklist_names:
            common_meta["blocklists"] = {
                "names": self.blocklist_names,
                "matches": blocklist_matches,
            }

        if blocklist_matches:
            samples = [
                {
                    "blocklist": m.get("blocklistName"),
                    "item_id": m.get("blocklistItemId"),
                    "matched_text": m.get("blocklistItemText"),
                }
                for m in blocklist_matches[:5]
            ]
            return self._block(
                text,
                reasons=[
                    f"blocklist '{m.get('blocklistName')}' matched: "
                    f"{m.get('blocklistItemText')!r}"
                    for m in blocklist_matches[:3]
                ],
                categories=["azure.blocklist"],
                score=1.0,
                metadata={**common_meta, "blocklist_samples": samples},
            )

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
    # text:detectProtectedMaterial (copyrighted text)
    # ------------------------------------------------------------------

    async def _run_protected_material(
        self, text: str, headers: dict[str, str]
    ) -> GuardCheckResult | None:
        url = content_safety_protected_material_url(self.endpoint, self.api_version)
        payload = {"text": text}
        body, err = await self._post_json(url, payload, headers=headers)
        if err is not None:
            log.warning("azure-content-safety protected-material error: %s", err)
            return None  # fail-soft for this sub-check; main analyze still runs

        assert body is not None
        analysis = body.get("protectedMaterialAnalysis") or {}
        detected = bool(analysis.get("detected", False))
        if not detected:
            return None

        return self._block(
            text,
            reasons=["protected-material: copyrighted text detected"],
            categories=["azure.protected_material"],
            score=1.0,
            metadata={
                "protected_material": analysis,
                "category_results": [
                    {"category": "ProtectedMaterial", "severity": 6, "passed": False},
                ],
                "check": "text:detectProtectedMaterial",
            },
        )

    # ------------------------------------------------------------------

    def _fail(self, text: str, *, reason: str) -> GuardCheckResult:
        # Surface the full list of checks the guard *would* have run so the
        # UI can render them as "skipped / not configured" pills.
        skipped: list[dict[str, Any]] = []
        if self.enable_prompt_shield:
            skipped.append(self._skipped_pill("PromptShield", reason))
        for cat in self.categories:
            skipped.append(self._skipped_pill(cat, reason))
        return self._fail_result(
            text,
            reason=reason,
            skipped_categories=skipped,
            extra_meta={"threshold": self.severity_threshold},
        )


register_guard("azure-content-safety", lambda cfg: AzureContentSafetyGuard(**cfg))
