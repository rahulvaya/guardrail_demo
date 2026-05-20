"""Base types for individual guardrails."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar


class GuardDecision(str, Enum):
    """A single guard's verdict on a piece of text."""

    ALLOW = "allow"          # text is fine, continue
    SANITIZE = "sanitize"    # text was rewritten; continue with sanitized_text
    BLOCK = "block"          # refuse the request; do NOT pass to next stage


class GuardStage(str, Enum):
    """Where in the pipeline a guard runs.

    Six explicit checkpoints (architecture diagram ①…⑥):

      ① API_INPUT    - request entering the public FastAPI / chat API.
      ② INPUT        - user message about to be sent to the LLM.
                       (alias: llm_input)
      ③ OUTPUT       - LLM reply on its way back to the orchestrator.
                       (alias: llm_output)
      ④ TOOL_INPUT   - planned tool call (name + arguments) about to be
                       executed against an external API / function.
      ⑤ TOOL_OUTPUT  - JSON returned by a tool before it is fed back to
                       the LLM as a `role: "tool"` message.
      ⑥ API_OUTPUT   - final assistant reply about to leave the public API.

      BOTH          - guard self-classifies as both input- and output-family.

    Input-family  = {API_INPUT, INPUT, TOOL_INPUT}
    Output-family = {OUTPUT, TOOL_OUTPUT, API_OUTPUT}
    """

    API_INPUT = "api_input"
    INPUT = "input"
    TOOL_INPUT = "tool_input"
    OUTPUT = "output"
    TOOL_OUTPUT = "tool_output"
    API_OUTPUT = "api_output"
    BOTH = "both"


INPUT_FAMILY: frozenset[GuardStage] = frozenset(
    {GuardStage.API_INPUT, GuardStage.INPUT, GuardStage.TOOL_INPUT}
)
OUTPUT_FAMILY: frozenset[GuardStage] = frozenset(
    {GuardStage.OUTPUT, GuardStage.TOOL_OUTPUT, GuardStage.API_OUTPUT}
)


def is_input_family(stage: GuardStage | str) -> bool:
    try:
        return GuardStage(stage) in INPUT_FAMILY
    except ValueError:
        return False


def is_output_family(stage: GuardStage | str) -> bool:
    try:
        return GuardStage(stage) in OUTPUT_FAMILY
    except ValueError:
        return False


@dataclass
class GuardCheckResult:
    """The result of a single guard's `check()` call."""

    guard_name: str
    decision: GuardDecision
    sanitized_text: str
    # Free-form reason strings, e.g. "matched-pattern: SSN"
    reasons: list[str] = field(default_factory=list)
    # Taxonomy-style category labels, e.g. ["pii.ssn", "harm.hate"]
    categories: list[str] = field(default_factory=list)
    # Numeric score (0..1) when the guard produces one (toxicity, similarity, ...).
    score: float | None = None
    # Whatever else the guard wants to record for tracing.
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return self.decision == GuardDecision.BLOCK


class Guard(ABC):
    """Abstract base for all guardrails.

    Subclasses MUST set the `name` class attribute (used in config and traces)
    and `stage` (when the guard runs). They implement `check()` and may
    override `aclose()` to release resources.
    """

    #: Stable identifier; appears in env flags (`GUARD_<NAME>_ENABLED`),
    #: pipeline traces, and the `/internal/guardrails/check` debug endpoint.
    name: ClassVar[str] = ""

    #: Where the guard runs. Most guards are INPUT or OUTPUT; rarely BOTH.
    stage: ClassVar[GuardStage] = GuardStage.INPUT

    #: Human-readable description shown in `/internal/guardrails/list`.
    description: ClassVar[str] = ""

    def __init__(self, **config: Any) -> None:
        self.config = config

    @abstractmethod
    async def check(self, text: str, *, context: dict[str, Any] | None = None) -> GuardCheckResult:
        """Inspect `text` and return a verdict.

        Implementations MUST be safe to call concurrently and MUST NOT raise
        on adversarial input (return `GuardDecision.BLOCK` with a reason
        instead).
        """

    async def aclose(self) -> None:
        """Release any resources (HTTP clients, model handles)."""

    # Convenience helpers for subclasses --------------------------------

    def _allow(self, text: str, **kw: Any) -> GuardCheckResult:
        return GuardCheckResult(guard_name=self.name, decision=GuardDecision.ALLOW, sanitized_text=text, **kw)

    def _sanitize(self, sanitized: str, *, reasons: list[str], **kw: Any) -> GuardCheckResult:
        return GuardCheckResult(
            guard_name=self.name,
            decision=GuardDecision.SANITIZE,
            sanitized_text=sanitized,
            reasons=reasons,
            **kw,
        )

    def _block(self, original: str, *, reasons: list[str], **kw: Any) -> GuardCheckResult:
        return GuardCheckResult(
            guard_name=self.name,
            decision=GuardDecision.BLOCK,
            sanitized_text=original,
            reasons=reasons,
            **kw,
        )
