"""Wire-contract models for the /v1/evaluate endpoint family."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

EvalStageLiteral = Literal[
    "api_input",
    "input",
    "llm_input",
    "tool_input",
    "output",
    "llm_output",
    "tool_output",
    "api_output",
]

EvaluatorCategory = Literal["safety", "quality", "nlp"]


class EvaluateRequest(BaseModel):
    """POST /v1/evaluate request body.

    At minimum provide `stage`, `query`, and `response`. Additional fields
    unlock more evaluators:
      - `context`      → enables groundedness evaluators.
      - `ground_truth` → enables NLP similarity metrics (BLEU/ROUGE/METEOR/GLEU).
      - `evaluators`   → restrict to a named subset; omit to run all available.
    """

    stage: EvalStageLiteral = Field(
        description="Pipeline checkpoint to evaluate: api_input|input|tool_input|output|tool_output|api_output"
    )
    query: str = Field(description="The user query or input text.")
    response: str = Field(description="The assistant/system response to evaluate.")
    context: str | None = Field(
        default=None,
        description="Grounding context (documents / facts). Required by groundedness evaluators.",
    )
    ground_truth: str | None = Field(
        default=None,
        description="Reference answer. Required by NLP similarity metrics (BLEU, ROUGE, METEOR, GLEU).",
    )
    evaluators: list[str] | None = Field(
        default=None,
        description=(
            "Evaluator names to run. Omit (or null) to run all available for this stage. "
            "Use GET /v1/evaluate/evaluators to list names."
        ),
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvaluatorResult(BaseModel):
    """Result of a single evaluator."""

    name: str
    category: EvaluatorCategory
    status: Literal["passed", "failed", "skipped", "error"] = "passed"
    score: float | None = Field(
        default=None,
        description="Numeric score. Safety: 0–7 (lower=safer). Quality: 1–5. NLP: 0–1.",
    )
    label: str | None = Field(
        default=None,
        description="Human-readable verdict, e.g. 'Safe', 'Unsafe', 'Very High', 'Low'.",
    )
    reason: str | None = Field(
        default=None,
        description="Model-generated explanation for the score.",
    )
    threshold: float | None = Field(
        default=None,
        description="Pass/fail threshold used for this result.",
    )
    raw: dict[str, Any] = Field(default_factory=dict, description="Raw SDK output.")
    error: str | None = None
    duration_ms: float = 0.0


class EvaluateSummary(BaseModel):
    """Aggregate counts rolled up from all evaluator results."""

    total: int
    passed: int
    failed: int
    skipped: int
    error: int
    safety_pass: bool
    quality_pass: bool
    avg_quality_score: float | None = None
    evaluators_run: list[str] = Field(default_factory=list)
    by_category: dict[str, dict[str, int]] = Field(default_factory=dict)


class EvaluateResponse(BaseModel):
    """POST /v1/evaluate response body."""

    stage: str
    query: str
    response: str
    overall_pass: bool
    safety_pass: bool
    quality_pass: bool
    summary: EvaluateSummary
    evaluator_results: list[EvaluatorResult] = Field(default_factory=list)
    failed_evaluators: list[str] = Field(default_factory=list)
    skipped_evaluators: list[str] = Field(default_factory=list)
    duration_ms: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvaluatorInfo(BaseModel):
    """Descriptor returned by GET /v1/evaluate/evaluators."""

    name: str
    category: EvaluatorCategory
    description: str
    stages: list[str]
    requires: list[str] = Field(
        description="What must be present: 'azure_ai_project', 'openai_model', 'context', 'ground_truth'."
    )
    available: bool = Field(description="True when required credentials are configured.")


# ---------------------------------------------------------------------------
# Batch models
# ---------------------------------------------------------------------------

class EvaluateBatchRequest(BaseModel):
    """POST /v1/evaluate/batch — evaluate multiple query/response pairs in one call.

    Each item is an independent :class:`EvaluateRequest`.  All items share the
    same ``evaluators`` filter when specified at the top level; per-item
    ``evaluators`` override that default.

    Example::

        {
          "evaluators": ["bleu-score", "rouge-score"],
          "items": [
            {
              "stage": "output",
              "query": "What is my balance?",
              "response": "Your balance is $500.",
              "ground_truth": "Balance is $500."
            },
            {
              "stage": "output",
              "query": "Transfer $200 to John.",
              "response": "Transfer of $200 to John completed.",
              "ground_truth": "Transfer completed."
            }
          ]
        }
    """

    items: list[EvaluateRequest] = Field(
        min_length=1,
        description="One or more evaluation requests. Each is evaluated independently.",
    )
    evaluators: list[str] | None = Field(
        default=None,
        description=(
            "Default evaluator filter applied to every item that does not supply its own. "
            "Omit to run all available evaluators."
        ),
    )


class EvaluateBatchItemResult(BaseModel):
    """Result for a single item within a batch."""

    index: int = Field(description="0-based position in the original items list.")
    result: EvaluateResponse


class EvaluateBatchSummary(BaseModel):
    """Rolled-up statistics across all batch items."""

    total_items: int
    items_passed: int
    items_failed: int
    total_evaluators_run: int
    total_evaluator_errors: int
    overall_pass: bool


class EvaluateBatchResponse(BaseModel):
    """POST /v1/evaluate/batch response body."""

    items: list[EvaluateBatchItemResult]
    summary: EvaluateBatchSummary
    duration_ms: float
