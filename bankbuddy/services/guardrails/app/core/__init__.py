"""BankBuddy guardrail framework.

A guardrail is a single, independently-testable check that inspects text
flowing into or out of the agent. Guards are composed by `GuardrailPipeline`,
which the agent calls before an LLM hop (input check) and after the final
assistant message (output check).

Design goals:
    * Each guard is enable-able / disable-able on its own (per-guard env flag).
    * Guards never raise on user input - they return a `GuardCheckResult`.
    * Guards can SANITIZE (rewrite the text) or BLOCK (refuse).
    * Heavy ML dependencies are optional and lazily imported.
    * Adding a custom guard is a single file - see
      `guards/banking_relevance.py` for the canonical example, and
      `docs/guardrails.md` for the full authoring guide.
"""
from .base import Guard, GuardCheckResult, GuardDecision, GuardStage
from .pipeline import GuardrailPipeline, PipelineResult
from .registry import build_pipeline_from_settings, register_guard

__all__ = [
    "Guard",
    "GuardCheckResult",
    "GuardDecision",
    "GuardStage",
    "GuardrailPipeline",
    "PipelineResult",
    "build_pipeline_from_settings",
    "register_guard",
]
