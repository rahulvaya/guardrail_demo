"""Azure AI Evaluation evaluator wrappers.

All 21 evaluators are implemented here:

Safety (require Azure AI Project — EVAL_AI_PROJECT_*):
  violence, sexual, self-harm, hate-unfairness,
  indirect-attack, protected-material

Quality / LLM-graded (require Azure OpenAI — EVAL_AZURE_OPENAI_*):
  groundedness, coherence, fluency, relevance, similarity, retrieval

NLP / local (no Azure — always available when the package is installed):
  bleu-score, gleu-score, meteor-score, rouge-score, f1-score

Composite (bundle multiple evaluators in a single SDK call):
  qa         — runs all quality evaluators (groundedness+coherence+fluency+relevance+similarity)
  content-safety — runs all safety evaluators (violence+sexual+self-harm+hate-unfairness)

Each wrapper:
  - declares `stages` (which pipeline checkpoints it applies to)
  - declares `requires` (credentials / extra fields needed)
  - instantiates the SDK evaluator lazily via `_build_sdk_evaluator()`
  - runs it in a thread-pool executor (the SDK is synchronous)
  - normalises the raw SDK dict into a clean EvaluatorResult
  - never raises — returns status="error"|"skipped" on any failure
"""
from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any

from ..core.observability import obs_log
from .models import EvaluatorResult
from .settings import EvaluationSettings

# ---------------------------------------------------------------------------
# Optional SDK import — package is not bundled in the base image so it may
# be absent in lightweight test environments.
# ---------------------------------------------------------------------------
try:
    import azure.ai.evaluation as _sdk

    _SDK_AVAILABLE = True
except ImportError:  # pragma: no cover
    _sdk = None  # type: ignore[assignment]
    _SDK_AVAILABLE = False

# Stage sets (canonical names, llm_* aliases are normalised by the caller)
_INPUT = frozenset({"api_input", "input", "tool_input"})
_OUTPUT = frozenset({"output", "tool_output", "api_output"})
_ALL = _INPUT | _OUTPUT


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class BaseEvaluatorWrapper(ABC):
    name: str = ""
    category: str = "custom"
    description: str = ""
    stages: frozenset[str] = _ALL
    requires: list[str] = []  # "azure_ai_project" | "openai_model" | "context" | "ground_truth"

    def __init__(self, settings: EvaluationSettings) -> None:
        self.settings = settings

    # ------------------------------------------------------------------ API

    def is_available(self) -> bool:
        if not _SDK_AVAILABLE:
            return False
        if "azure_ai_project" in self.requires and not self.settings.has_azure_ai_project:
            return False
        if "openai_model" in self.requires and not self.settings.has_openai_config:
            return False
        return True

    async def evaluate(
        self,
        *,
        query: str,
        response: str,
        context: str | None = None,
        ground_truth: str | None = None,
    ) -> EvaluatorResult:
        # ---- pre-flight checks ----
        if not _SDK_AVAILABLE:
            return self._skipped("azure-ai-evaluation package not installed")
        if "azure_ai_project" in self.requires and not self.settings.has_azure_ai_project:
            return self._skipped("EVAL_AI_PROJECT_* env vars not configured")
        if "openai_model" in self.requires and not self.settings.has_openai_config:
            return self._skipped("EVAL_AZURE_OPENAI_* env vars not configured")
        if "context" in self.requires and not context:
            return self._skipped("Requires `context` (grounding sources) but none provided")
        if "ground_truth" in self.requires and not ground_truth:
            return self._skipped("Requires `ground_truth` reference answer but none provided")

        # ---- build SDK evaluator ----
        try:
            evaluator = self._build_sdk_evaluator()
        except Exception as exc:  # noqa: BLE001
            obs_log(
                "evaluation.build_error",
                level="warning",
                evaluator=self.name,
                error=str(exc)[:200],
                exc_info=True,
            )
            return EvaluatorResult(
                name=self.name,
                category=self.category,  # type: ignore[arg-type]
                status="error",
                error=f"Build failed — {type(exc).__name__}: {str(exc)[:200]}",
            )

        # ---- run (in thread pool — SDK is synchronous) ----
        kwargs = self._build_kwargs(
            query=query, response=response, context=context, ground_truth=ground_truth
        )
        t0 = time.perf_counter()
        try:
            loop = asyncio.get_event_loop()
            raw: dict[str, Any] = await loop.run_in_executor(
                None, lambda: evaluator(**kwargs)
            )
            duration_ms = (time.perf_counter() - t0) * 1000
            result = self._parse_result(raw, duration_ms)
            obs_log(
                "evaluation.evaluator_done",
                evaluator=self.name,
                status=result.status,
                score=result.score,
                duration_ms=round(duration_ms, 1),
            )
            return result
        except Exception as exc:  # noqa: BLE001
            duration_ms = (time.perf_counter() - t0) * 1000
            obs_log(
                "evaluation.evaluator_error",
                level="warning",
                evaluator=self.name,
                error_type=type(exc).__name__,
                duration_ms=round(duration_ms, 1),
                exc_info=True,
            )
            return EvaluatorResult(
                name=self.name,
                category=self.category,  # type: ignore[arg-type]
                status="error",
                error=f"{type(exc).__name__}: {str(exc)[:300]}",
                duration_ms=duration_ms,
            )

    # ------------------------------------------------------------------ Overridable helpers

    @abstractmethod
    def _build_sdk_evaluator(self) -> Any:
        """Instantiate and return the SDK evaluator object."""

    @abstractmethod
    def _parse_result(self, raw: dict[str, Any], duration_ms: float) -> EvaluatorResult:
        """Map the raw SDK result dict → EvaluatorResult."""

    def _build_kwargs(
        self,
        *,
        query: str,
        response: str,
        context: str | None,
        ground_truth: str | None,
    ) -> dict[str, Any]:
        return {"query": query, "response": response}

    # ------------------------------------------------------------------ Internal utils

    def _skipped(self, reason: str) -> EvaluatorResult:
        return EvaluatorResult(
            name=self.name,
            category=self.category,  # type: ignore[arg-type]
            status="skipped",
            reason=reason,
        )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _safety_score_label(score: float | None) -> str | None:
    """Map Azure Content Safety severity (0–7) to a human label."""
    if score is None:
        return None
    s = float(score)
    if s < 1:
        return "Safe"
    elif s < 3:
        return "Low"
    elif s < 5:
        return "Medium"
    else:
        return "High"


def _quality_score_label(score: float | None) -> str | None:
    """Map 1–5 quality score to a human label."""
    if score is None:
        return None
    s = float(score)
    if s >= 4.5:
        return "Very High"
    elif s >= 3.5:
        return "High"
    elif s >= 2.5:
        return "Medium"
    elif s >= 1.5:
        return "Low"
    else:
        return "Very Low"


# ---------------------------------------------------------------------------
# Safety evaluators  (require Azure AI Project)
# ---------------------------------------------------------------------------

_RAI_UNAVAILABLE_MSG = (
    "RAI service returned no results — the Content Safety / RAI evaluators are not "
    "available in this region or subscription. See https://aka.ms/azsdk/python/evaluation/"
    "safetyevaluator/troubleshoot for supported regions."
)

# Azure Content Safety API severity levels (0/2/4/6)
def _cs_severity_label(severity: int) -> str:
    if severity == 0:
        return "Safe"
    elif severity == 2:
        return "Low"
    elif severity == 4:
        return "Medium"
    return "High"


class SafetyEvaluatorBase(BaseEvaluatorWrapper):
    """Shared base for all Azure RAI safety evaluators.

    When the Foundry RAI service returns an empty dict (unavailable in the region),
    evaluators with ``_cs_category`` set fall back to the Azure Content Safety
    ``text:analyze`` API using the EVAL_CONTENT_SAFETY_ENDPOINT resource.
    Evaluators without a CS mapping (indirect-attack, protected-material) skip gracefully.
    """

    # Subclasses set this to the Content Safety API category name(s) to use as
    # fallback.  None means no direct CS mapping — evaluator skips when RAI is down.
    _cs_categories: list[str] | None = None

    async def evaluate(
        self,
        *,
        query: str,
        response: str,
        context: str | None = None,
        ground_truth: str | None = None,
    ) -> EvaluatorResult:
        result = await super().evaluate(
            query=query, response=response, context=context, ground_truth=ground_truth
        )
        # If RAI returned "skipped" (empty result) and we have a CS endpoint + categories,
        # fall back to the Content Safety direct API.
        if (
            result.status == "skipped"
            and _RAI_UNAVAILABLE_MSG in (result.reason or "")
            and self._cs_categories
            and self.settings.has_content_safety_direct
        ):
            cs_result = await self._call_content_safety_api(
                response, self._cs_categories, result.duration_ms or 0.0
            )
            if cs_result is not None:
                return cs_result
        return result

    async def _call_content_safety_api(
        self,
        text: str,
        categories: list[str],
        prior_duration_ms: float,
    ) -> EvaluatorResult | None:
        """Call POST /contentsafety/text:analyze and return a parsed EvaluatorResult."""
        import httpx

        url = self.settings.content_safety_analyze_url
        if not url:
            return None

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.settings.azure_content_safety_aad_token:
            headers["Authorization"] = f"Bearer {self.settings.azure_content_safety_aad_token}"
        elif self.settings.azure_content_safety_key:
            headers["Ocp-Apim-Subscription-Key"] = self.settings.azure_content_safety_key
        else:
            return None

        payload = {
            "text": text[:10000],  # Content Safety API 10K char limit
            "categories": categories,
            "outputType": "FourSeverityLevels",
        }

        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001
            obs_log(
                "evaluation.cs_direct_error",
                level="warning",
                evaluator=self.name,
                error=type(exc).__name__,
                detail=str(exc)[:200],
            )
            return None

        cs_duration_ms = (time.perf_counter() - t0) * 1000 + prior_duration_ms
        return self._parse_cs_result(data, cs_duration_ms)

    def _parse_cs_result(self, data: dict, duration_ms: float) -> EvaluatorResult:
        """Parse Content Safety text:analyze response into an EvaluatorResult.

        Subclasses may override to extract a single category's score.
        Default: uses the max severity across all returned categories.
        """
        items = data.get("categoriesAnalysis", [])
        scores = {c["category"]: c["severity"] for c in items if "category" in c}
        max_sev = max(scores.values(), default=0)
        passed = max_sev < 4  # <4 = Safe/Low → pass; ≥4 = Medium/High → fail
        worst = max(scores, key=scores.__getitem__) if scores else None
        summary = ", ".join(f"{k}={v}" for k, v in scores.items())
        label = _cs_severity_label(max_sev)
        if worst and len(scores) > 1:
            label = f"{label} (worst: {worst})"
        return EvaluatorResult(
            name=self.name,
            category="safety",
            status="passed" if passed else "failed",
            score=float(max_sev),
            label=label,
            reason=summary or None,
            threshold=4.0,
            raw=data,
            duration_ms=duration_ms,
        )

    def _rai_empty_check(self, raw: dict, duration_ms: float) -> EvaluatorResult | None:
        """Return a skipped sentinel if the RAI SDK returned nothing, else None."""
        if not raw:
            return EvaluatorResult(
                name=self.name,
                category="safety",
                status="skipped",
                reason=_RAI_UNAVAILABLE_MSG,
                duration_ms=duration_ms,
            )
        return None


class ViolenceEvaluatorWrapper(SafetyEvaluatorBase):
    name = "violence"
    category = "safety"
    description = "Detects violent content. Score 0–7; Safe=0."
    stages = _ALL
    requires = ["azure_ai_project"]
    _cs_categories = ["Violence"]

    def _parse_cs_result(self, data: dict, duration_ms: float) -> EvaluatorResult:
        sev = next((c["severity"] for c in data.get("categoriesAnalysis", []) if c["category"] == "Violence"), 0)
        return EvaluatorResult(name=self.name, category="safety",
            status="passed" if sev < 4 else "failed",
            score=float(sev), label=_cs_severity_label(sev),
            threshold=4.0, raw=data, duration_ms=duration_ms)

    def _build_sdk_evaluator(self) -> Any:
        return _sdk.ViolenceEvaluator(
            credential=self.settings.get_credential(),
            azure_ai_project=self.settings.build_azure_ai_project(),
        )

    def _parse_result(self, raw: dict, duration_ms: float) -> EvaluatorResult:
        if skipped := self._rai_empty_check(raw, duration_ms):
            return skipped
        label = raw.get("violence", "")
        score = raw.get("violence_score")
        reason = raw.get("violence_reason", "") or ""
        passed = str(label).lower() in ("safe",) or (
            score is not None and float(score) < 3
        )
        return EvaluatorResult(
            name=self.name,
            category="safety",
            status="passed" if passed else "failed",
            score=float(score) if score is not None else None,
            label=_safety_score_label(score) or str(label),
            reason=reason,
            threshold=3.0,
            raw=raw,
            duration_ms=duration_ms,
        )


class SexualEvaluatorWrapper(SafetyEvaluatorBase):
    name = "sexual"
    category = "safety"
    description = "Detects sexual content. Score 0–7; Safe=0."
    stages = _ALL
    requires = ["azure_ai_project"]
    _cs_categories = ["Sexual"]

    def _parse_cs_result(self, data: dict, duration_ms: float) -> EvaluatorResult:
        sev = next((c["severity"] for c in data.get("categoriesAnalysis", []) if c["category"] == "Sexual"), 0)
        return EvaluatorResult(name=self.name, category="safety",
            status="passed" if sev < 4 else "failed",
            score=float(sev), label=_cs_severity_label(sev),
            threshold=4.0, raw=data, duration_ms=duration_ms)

    def _build_sdk_evaluator(self) -> Any:
        return _sdk.SexualEvaluator(
            credential=self.settings.get_credential(),
            azure_ai_project=self.settings.build_azure_ai_project(),
        )

    def _parse_result(self, raw: dict, duration_ms: float) -> EvaluatorResult:
        if skipped := self._rai_empty_check(raw, duration_ms):
            return skipped
        label = raw.get("sexual", "")
        score = raw.get("sexual_score")
        reason = raw.get("sexual_reason", "") or ""
        passed = str(label).lower() in ("safe",) or (
            score is not None and float(score) < 3
        )
        return EvaluatorResult(
            name=self.name,
            category="safety",
            status="passed" if passed else "failed",
            score=float(score) if score is not None else None,
            label=_safety_score_label(score) or str(label),
            reason=reason,
            threshold=3.0,
            raw=raw,
            duration_ms=duration_ms,
        )


class SelfHarmEvaluatorWrapper(SafetyEvaluatorBase):
    name = "self-harm"
    category = "safety"
    description = "Detects self-harm content. Score 0–7; Safe=0."
    stages = _ALL
    requires = ["azure_ai_project"]
    _cs_categories = ["SelfHarm"]

    def _parse_cs_result(self, data: dict, duration_ms: float) -> EvaluatorResult:
        sev = next((c["severity"] for c in data.get("categoriesAnalysis", []) if c["category"] == "SelfHarm"), 0)
        return EvaluatorResult(name=self.name, category="safety",
            status="passed" if sev < 4 else "failed",
            score=float(sev), label=_cs_severity_label(sev),
            threshold=4.0, raw=data, duration_ms=duration_ms)

    def _build_sdk_evaluator(self) -> Any:
        return _sdk.SelfHarmEvaluator(
            credential=self.settings.get_credential(),
            azure_ai_project=self.settings.build_azure_ai_project(),
        )

    def _parse_result(self, raw: dict, duration_ms: float) -> EvaluatorResult:
        if skipped := self._rai_empty_check(raw, duration_ms):
            return skipped
        label = raw.get("self_harm", raw.get("self-harm", ""))
        score = raw.get("self_harm_score", raw.get("self-harm_score"))
        reason = raw.get("self_harm_reason", raw.get("self-harm_reason", "")) or ""
        passed = str(label).lower() in ("safe",) or (
            score is not None and float(score) < 3
        )
        return EvaluatorResult(
            name=self.name,
            category="safety",
            status="passed" if passed else "failed",
            score=float(score) if score is not None else None,
            label=_safety_score_label(score) or str(label),
            reason=reason,
            threshold=3.0,
            raw=raw,
            duration_ms=duration_ms,
        )


class HateUnfairnessEvaluatorWrapper(SafetyEvaluatorBase):
    name = "hate-unfairness"
    category = "safety"
    description = "Detects hate speech and unfairness. Score 0–7; Safe=0."
    stages = _ALL
    requires = ["azure_ai_project"]
    _cs_categories = ["Hate"]

    def _parse_cs_result(self, data: dict, duration_ms: float) -> EvaluatorResult:
        sev = next((c["severity"] for c in data.get("categoriesAnalysis", []) if c["category"] == "Hate"), 0)
        return EvaluatorResult(name=self.name, category="safety",
            status="passed" if sev < 4 else "failed",
            score=float(sev), label=_cs_severity_label(sev),
            threshold=4.0, raw=data, duration_ms=duration_ms)

    def _build_sdk_evaluator(self) -> Any:
        return _sdk.HateUnfairnessEvaluator(
            credential=self.settings.get_credential(),
            azure_ai_project=self.settings.build_azure_ai_project(),
        )

    def _parse_result(self, raw: dict, duration_ms: float) -> EvaluatorResult:
        if skipped := self._rai_empty_check(raw, duration_ms):
            return skipped
        label = raw.get("hate_unfairness", raw.get("hate-unfairness", ""))
        score = raw.get("hate_unfairness_score", raw.get("hate-unfairness_score"))
        reason = (
            raw.get("hate_unfairness_reason", raw.get("hate-unfairness_reason", ""))
            or ""
        )
        passed = str(label).lower() in ("safe",) or (
            score is not None and float(score) < 3
        )
        return EvaluatorResult(
            name=self.name,
            category="safety",
            status="passed" if passed else "failed",
            score=float(score) if score is not None else None,
            label=_safety_score_label(score) or str(label),
            reason=reason,
            threshold=3.0,
            raw=raw,
            duration_ms=duration_ms,
        )


class IndirectAttackEvaluatorWrapper(SafetyEvaluatorBase):
    name = "indirect-attack"
    category = "safety"
    description = "Detects cross-domain prompt injection (XPIA) in text."
    stages = _INPUT | frozenset({"tool_output"})
    requires = ["azure_ai_project"]

    def _build_sdk_evaluator(self) -> Any:
        if not hasattr(_sdk, "IndirectAttackEvaluator"):
            raise ImportError(
                "IndirectAttackEvaluator not found in azure-ai-evaluation. "
                "Upgrade to >=1.0.0."
            )
        return _sdk.IndirectAttackEvaluator(
            credential=self.settings.get_credential(),
            azure_ai_project=self.settings.build_azure_ai_project(),
        )

    def _parse_result(self, raw: dict, duration_ms: float) -> EvaluatorResult:
        if skipped := self._rai_empty_check(raw, duration_ms):
            return skipped
        label = str(raw.get("indirect_attack", "False"))
        reason = raw.get("indirect_attack_reason", "") or ""
        detected = label.lower() in ("true", "1", "yes", "attack")
        return EvaluatorResult(
            name=self.name,
            category="safety",
            status="failed" if detected else "passed",
            label="Attack detected" if detected else "No attack",
            reason=reason,
            raw=raw,
            duration_ms=duration_ms,
        )


class ProtectedMaterialEvaluatorWrapper(SafetyEvaluatorBase):
    name = "protected-material"
    category = "safety"
    description = "Detects copyrighted / protected material in LLM output."
    stages = _OUTPUT
    requires = ["azure_ai_project"]

    def _build_sdk_evaluator(self) -> Any:
        return _sdk.ProtectedMaterialEvaluator(
            credential=self.settings.get_credential(),
            azure_ai_project=self.settings.build_azure_ai_project(),
        )

    def _parse_result(self, raw: dict, duration_ms: float) -> EvaluatorResult:
        if skipped := self._rai_empty_check(raw, duration_ms):
            return skipped
        label = str(
            raw.get("protected_material", raw.get("protected-material", "False"))
        )
        reason = (
            raw.get(
                "protected_material_reason",
                raw.get("protected-material_reason", ""),
            )
            or ""
        )
        detected = label.lower() in ("true", "1", "yes")
        return EvaluatorResult(
            name=self.name,
            category="safety",
            status="failed" if detected else "passed",
            label="Protected material detected" if detected else "Clean",
            reason=reason,
            raw=raw,
            duration_ms=duration_ms,
        )


# ---------------------------------------------------------------------------
# Quality / LLM-graded evaluators  (require Azure OpenAI)
# ---------------------------------------------------------------------------

class GroundednessEvaluatorWrapper(BaseEvaluatorWrapper):
    name = "groundedness"
    category = "quality"
    description = "Scores how well the response is grounded in the provided context (1–5)."
    stages = _OUTPUT
    requires = ["openai_model", "context"]

    def _build_sdk_evaluator(self) -> Any:
        return _sdk.GroundednessEvaluator(
            model_config=self.settings.build_model_config_dict(),
            credential=self.settings.get_credential(),
        )

    def _build_kwargs(self, *, query, response, context, ground_truth) -> dict:
        return {"query": query, "response": response, "context": context}

    def _parse_result(self, raw: dict, duration_ms: float) -> EvaluatorResult:
        score = raw.get("groundedness")
        reason = raw.get("groundedness_reason", "") or ""
        passed = score is not None and float(score) >= 3.0
        return EvaluatorResult(
            name=self.name,
            category="quality",
            status="passed" if passed else "failed",
            score=float(score) if score is not None else None,
            label=_quality_score_label(score),
            reason=reason,
            threshold=3.0,
            raw=raw,
            duration_ms=duration_ms,
        )


class CoherenceEvaluatorWrapper(BaseEvaluatorWrapper):
    name = "coherence"
    category = "quality"
    description = "Scores the logical coherence of the response (1–5)."
    stages = _ALL
    requires = ["openai_model"]

    def _build_sdk_evaluator(self) -> Any:
        return _sdk.CoherenceEvaluator(
            model_config=self.settings.build_model_config_dict(),
            credential=self.settings.get_credential(),
        )

    def _parse_result(self, raw: dict, duration_ms: float) -> EvaluatorResult:
        score = raw.get("coherence")
        reason = raw.get("coherence_reason", "") or ""
        passed = score is not None and float(score) >= 3.0
        return EvaluatorResult(
            name=self.name,
            category="quality",
            status="passed" if passed else "failed",
            score=float(score) if score is not None else None,
            label=_quality_score_label(score),
            reason=reason,
            threshold=3.0,
            raw=raw,
            duration_ms=duration_ms,
        )


class FluencyEvaluatorWrapper(BaseEvaluatorWrapper):
    name = "fluency"
    category = "quality"
    description = "Scores the linguistic fluency of the response (1–5)."
    stages = _OUTPUT
    requires = ["openai_model"]

    def _build_sdk_evaluator(self) -> Any:
        return _sdk.FluencyEvaluator(
            model_config=self.settings.build_model_config_dict(),
            credential=self.settings.get_credential(),
        )

    def _build_kwargs(self, *, query, response, context, ground_truth) -> dict:
        return {"response": response}

    def _parse_result(self, raw: dict, duration_ms: float) -> EvaluatorResult:
        score = raw.get("fluency")
        reason = raw.get("fluency_reason", "") or ""
        passed = score is not None and float(score) >= 3.0
        return EvaluatorResult(
            name=self.name,
            category="quality",
            status="passed" if passed else "failed",
            score=float(score) if score is not None else None,
            label=_quality_score_label(score),
            reason=reason,
            threshold=3.0,
            raw=raw,
            duration_ms=duration_ms,
        )


class RelevanceEvaluatorWrapper(BaseEvaluatorWrapper):
    name = "relevance"
    category = "quality"
    description = "Scores how relevant the response is to the query (1–5)."
    stages = _ALL
    requires = ["openai_model"]

    def _build_sdk_evaluator(self) -> Any:
        return _sdk.RelevanceEvaluator(
            model_config=self.settings.build_model_config_dict(),
            credential=self.settings.get_credential(),
        )

    def _parse_result(self, raw: dict, duration_ms: float) -> EvaluatorResult:
        score = raw.get("relevance")
        reason = raw.get("relevance_reason", "") or ""
        passed = score is not None and float(score) >= 3.0
        return EvaluatorResult(
            name=self.name,
            category="quality",
            status="passed" if passed else "failed",
            score=float(score) if score is not None else None,
            label=_quality_score_label(score),
            reason=reason,
            threshold=3.0,
            raw=raw,
            duration_ms=duration_ms,
        )


class SimilarityEvaluatorWrapper(BaseEvaluatorWrapper):
    name = "similarity"
    category = "quality"
    description = "Scores semantic similarity between response and ground truth (1–5)."
    stages = _OUTPUT
    requires = ["openai_model", "ground_truth"]

    def _build_sdk_evaluator(self) -> Any:
        return _sdk.SimilarityEvaluator(
            model_config=self.settings.build_model_config_dict(),
            credential=self.settings.get_credential(),
        )

    def _build_kwargs(self, *, query, response, context, ground_truth) -> dict:
        return {"query": query, "response": response, "ground_truth": ground_truth}

    def _parse_result(self, raw: dict, duration_ms: float) -> EvaluatorResult:
        score = raw.get("similarity")
        reason = raw.get("similarity_reason", "") or ""
        passed = score is not None and float(score) >= 3.0
        return EvaluatorResult(
            name=self.name,
            category="quality",
            status="passed" if passed else "failed",
            score=float(score) if score is not None else None,
            label=_quality_score_label(score),
            reason=reason,
            threshold=3.0,
            raw=raw,
            duration_ms=duration_ms,
        )


# ---------------------------------------------------------------------------
# NLP / local evaluators  (no Azure required — always available when SDK installed)
# ---------------------------------------------------------------------------

class _NLPBase(BaseEvaluatorWrapper):
    """Base for NLP evaluators that need ground_truth but no Azure credentials."""

    category = "nlp"
    stages = _OUTPUT
    requires = ["ground_truth"]

    def is_available(self) -> bool:
        return _SDK_AVAILABLE  # no Azure creds needed

    def _build_kwargs(self, *, query, response, context, ground_truth) -> dict:
        return {"response": response, "ground_truth": ground_truth}


class BleuScoreEvaluatorWrapper(_NLPBase):
    name = "bleu-score"
    description = "BLEU n-gram precision overlap between response and ground truth (0–1)."

    def _build_sdk_evaluator(self) -> Any:
        return _sdk.BleuScoreEvaluator()

    def _parse_result(self, raw: dict, duration_ms: float) -> EvaluatorResult:
        score = raw.get("bleu_score")
        return EvaluatorResult(
            name=self.name,
            category="nlp",
            status="passed" if (score is not None and float(score) > 0.1) else "failed",
            score=float(score) if score is not None else None,
            label=f"{float(score):.3f}" if score is not None else None,
            threshold=0.1,
            raw=raw,
            duration_ms=duration_ms,
        )


class GleuScoreEvaluatorWrapper(_NLPBase):
    name = "gleu-score"
    description = "GLEU sentence-level BLEU variant (0–1)."

    def _build_sdk_evaluator(self) -> Any:
        return _sdk.GleuScoreEvaluator()

    def _parse_result(self, raw: dict, duration_ms: float) -> EvaluatorResult:
        score = raw.get("gleu_score")
        return EvaluatorResult(
            name=self.name,
            category="nlp",
            status="passed" if (score is not None and float(score) > 0.1) else "failed",
            score=float(score) if score is not None else None,
            label=f"{float(score):.3f}" if score is not None else None,
            threshold=0.1,
            raw=raw,
            duration_ms=duration_ms,
        )


class MeteorScoreEvaluatorWrapper(_NLPBase):
    name = "meteor-score"
    description = "METEOR semantic overlap score between response and ground truth (0–1)."

    def _build_sdk_evaluator(self) -> Any:
        return _sdk.MeteorScoreEvaluator()

    def _parse_result(self, raw: dict, duration_ms: float) -> EvaluatorResult:
        score = raw.get("meteor_score")
        return EvaluatorResult(
            name=self.name,
            category="nlp",
            status="passed" if (score is not None and float(score) > 0.1) else "failed",
            score=float(score) if score is not None else None,
            label=f"{float(score):.3f}" if score is not None else None,
            threshold=0.1,
            raw=raw,
            duration_ms=duration_ms,
        )


class RougeScoreEvaluatorWrapper(_NLPBase):
    name = "rouge-score"
    description = "ROUGE-L recall-oriented overlap between response and ground truth (0–1)."

    def _build_sdk_evaluator(self) -> Any:
        # RougeScoreEvaluator requires a rouge_type argument in azure-ai-evaluation 1.x.
        # "rouge_l" measures longest common subsequence F1.
        # RougeType enum is internal; use string value 'rougeL' (capital L, no underscore).
        try:
            from azure.ai.evaluation._evaluators._rouge._rouge import RougeType  # type: ignore
            return _sdk.RougeScoreEvaluator(rouge_type=RougeType.ROUGE_L)
        except Exception:
            return _sdk.RougeScoreEvaluator(rouge_type="rougeL")

    def _parse_result(self, raw: dict, duration_ms: float) -> EvaluatorResult:
        # SDK may return rouge_score, rouge_f1, or rouge_l depending on version.
        score = (
            raw.get("rouge_score")
            or raw.get("rouge_f1")
            or raw.get("rouge_l")
        )
        return EvaluatorResult(
            name=self.name,
            category="nlp",
            status="passed" if (score is not None and float(score) > 0.1) else "failed",
            score=float(score) if score is not None else None,
            label=f"{float(score):.3f}" if score is not None else None,
            threshold=0.1,
            raw=raw,
            duration_ms=duration_ms,
        )


class F1ScoreEvaluatorWrapper(_NLPBase):
    name = "f1-score"
    description = "Token-level F1 overlap between response and ground truth (0–1)."

    def _build_sdk_evaluator(self) -> Any:
        return _sdk.F1ScoreEvaluator()

    def _parse_result(self, raw: dict, duration_ms: float) -> EvaluatorResult:
        score = raw.get("f1_score")
        return EvaluatorResult(
            name=self.name,
            category="nlp",
            status="passed" if (score is not None and float(score) > 0.1) else "failed",
            score=float(score) if score is not None else None,
            label=f"{float(score):.3f}" if score is not None else None,
            threshold=0.1,
            raw=raw,
            duration_ms=duration_ms,
        )


# ---------------------------------------------------------------------------
# Quality — Retrieval evaluator  (AI-assisted, requires context)
# ---------------------------------------------------------------------------

class RetrievalEvaluatorWrapper(BaseEvaluatorWrapper):
    name = "retrieval"
    category = "quality"
    description = (
        "Scores the quality of retrieved context chunks relative to the query (1–5). "
        "Higher = more relevant retrieval."
    )
    stages = _OUTPUT
    requires = ["openai_model", "context"]

    def _build_sdk_evaluator(self) -> Any:
        if not hasattr(_sdk, "RetrievalEvaluator"):
            raise ImportError(
                "RetrievalEvaluator not found in azure-ai-evaluation. "
                "Upgrade to >=1.0.0."
            )
        return _sdk.RetrievalEvaluator(
            model_config=self.settings.build_model_config_dict(),
            credential=self.settings.get_credential(),
        )

    def _build_kwargs(self, *, query, response, context, ground_truth) -> dict:
        return {"query": query, "context": context}

    def _parse_result(self, raw: dict, duration_ms: float) -> EvaluatorResult:
        score = raw.get("retrieval")
        reason = raw.get("retrieval_reason", "") or ""
        passed = score is not None and float(score) >= 3.0
        return EvaluatorResult(
            name=self.name,
            category="quality",
            status="passed" if passed else "failed",
            score=float(score) if score is not None else None,
            label=_quality_score_label(score),
            reason=reason,
            threshold=3.0,
            raw=raw,
            duration_ms=duration_ms,
        )


# ---------------------------------------------------------------------------
# Composite evaluators
# ---------------------------------------------------------------------------

class QAEvaluatorWrapper(BaseEvaluatorWrapper):
    """Composite: runs Groundedness + Coherence + Fluency + Relevance + Similarity.

    The SDK `QAEvaluator` returns a flat dict with all sub-evaluator keys.
    We aggregate them into a single EvaluatorResult whose score is the average
    of sub-scores and whose status is failed if any sub-evaluator fails.
    """

    name = "qa"
    category = "quality"
    description = (
        "Composite quality evaluator: runs Groundedness, Coherence, Fluency, "
        "Relevance, and Similarity in one SDK call. Score is the average (1–5)."
    )
    stages = _OUTPUT
    requires = ["openai_model", "context", "ground_truth"]

    def _build_sdk_evaluator(self) -> Any:
        if not hasattr(_sdk, "QAEvaluator"):
            raise ImportError(
                "QAEvaluator not found in azure-ai-evaluation. Upgrade to >=1.0.0."
            )
        return _sdk.QAEvaluator(
            model_config=self.settings.build_model_config_dict(),
            credential=self.settings.get_credential(),
        )

    def _build_kwargs(self, *, query, response, context, ground_truth) -> dict:
        return {
            "query": query,
            "response": response,
            "context": context,
            "ground_truth": ground_truth,
        }

    def _parse_result(self, raw: dict, duration_ms: float) -> EvaluatorResult:
        sub_keys = ["groundedness", "coherence", "fluency", "relevance", "similarity"]
        scores = [
            float(raw[k]) for k in sub_keys if k in raw and raw[k] is not None
        ]
        avg = round(sum(scores) / len(scores), 2) if scores else None
        passed = avg is not None and avg >= 3.0
        sub_summary = ", ".join(
            f"{k}={raw[k]:.1f}" for k in sub_keys if k in raw and raw[k] is not None
        )
        return EvaluatorResult(
            name=self.name,
            category="quality",
            status="passed" if passed else "failed",
            score=avg,
            label=_quality_score_label(avg),
            reason=sub_summary or None,
            threshold=3.0,
            raw=raw,
            duration_ms=duration_ms,
        )


class ContentSafetyEvaluatorWrapper(SafetyEvaluatorBase):
    """Composite: runs Violence + Sexual + SelfHarm + HateUnfairness together.

    The SDK `ContentSafetyEvaluator` returns a flat dict with all sub-evaluator
    keys.  We fail if any sub-evaluator reports a score >= 3 (Medium or higher).
    When the Foundry RAI service is unavailable, falls back to Content Safety
    text:analyze with all four categories.
    """

    name = "content-safety"
    category = "safety"
    description = (
        "Composite safety evaluator: runs Violence, Sexual, Self-Harm, and Hate-Unfairness "
        "in one SDK call. Fails if any dimension scores Medium (≥3) or above."
    )
    stages = _ALL
    requires = ["azure_ai_project"]
    _cs_categories = ["Hate", "SelfHarm", "Sexual", "Violence"]

    def _build_sdk_evaluator(self) -> Any:
        if not hasattr(_sdk, "ContentSafetyEvaluator"):
            raise ImportError(
                "ContentSafetyEvaluator not found in azure-ai-evaluation. "
                "Upgrade to >=1.0.0."
            )
        return _sdk.ContentSafetyEvaluator(
            credential=self.settings.get_credential(),
            azure_ai_project=self.settings.build_azure_ai_project(),
        )

    def _parse_result(self, raw: dict, duration_ms: float) -> EvaluatorResult:
        if skipped := self._rai_empty_check(raw, duration_ms):
            return skipped
        sub_dimensions = ["violence", "sexual", "self_harm", "hate_unfairness"]
        sub_scores: dict[str, float] = {}
        for dim in sub_dimensions:
            score_key = f"{dim}_score"
            if score_key in raw and raw[score_key] is not None:
                sub_scores[dim] = float(raw[score_key])

        max_score = max(sub_scores.values()) if sub_scores else None
        passed = max_score is None or max_score < 3.0
        worst = max(sub_scores, key=sub_scores.__getitem__) if sub_scores else None
        sub_summary = ", ".join(f"{k}={v:.0f}" for k, v in sub_scores.items())
        label = (
            f"{_safety_score_label(max_score)} (worst: {worst})"
            if max_score is not None and worst
            else _safety_score_label(max_score)
        )
        return EvaluatorResult(
            name=self.name,
            category="safety",
            status="passed" if passed else "failed",
            score=max_score,
            label=label,
            reason=sub_summary or None,
            threshold=3.0,
            raw=raw,
            duration_ms=duration_ms,
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_ALL_WRAPPER_CLASSES: list[type[BaseEvaluatorWrapper]] = [
    # Safety
    ViolenceEvaluatorWrapper,
    SexualEvaluatorWrapper,
    SelfHarmEvaluatorWrapper,
    HateUnfairnessEvaluatorWrapper,
    IndirectAttackEvaluatorWrapper,
    ProtectedMaterialEvaluatorWrapper,
    # Quality (AI-assisted)
    GroundednessEvaluatorWrapper,
    CoherenceEvaluatorWrapper,
    FluencyEvaluatorWrapper,
    RelevanceEvaluatorWrapper,
    SimilarityEvaluatorWrapper,
    RetrievalEvaluatorWrapper,
    # NLP (local)
    BleuScoreEvaluatorWrapper,
    GleuScoreEvaluatorWrapper,
    MeteorScoreEvaluatorWrapper,
    RougeScoreEvaluatorWrapper,
    F1ScoreEvaluatorWrapper,
    # Composite
    QAEvaluatorWrapper,
    ContentSafetyEvaluatorWrapper,
]

EVALUATOR_REGISTRY: dict[str, type[BaseEvaluatorWrapper]] = {
    cls.name: cls for cls in _ALL_WRAPPER_CLASSES
}


def get_evaluators_for_stage(
    stage: str,
    settings: EvaluationSettings,
    requested: list[str] | None = None,
) -> list[BaseEvaluatorWrapper]:
    """Return wrapper instances applicable to `stage`, filtered by `requested` names."""
    # Normalise llm_* aliases to canonical names
    canonical = stage.replace("llm_input", "input").replace("llm_output", "output")
    result = []
    for cls in _ALL_WRAPPER_CLASSES:
        if canonical not in cls.stages:
            continue
        if requested is not None and cls.name not in requested:
            continue
        result.append(cls(settings))
    return result
