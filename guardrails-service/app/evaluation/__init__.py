"""Azure AI Evaluation sub-package.

Exposes everything the FastAPI routes need:

    from .evaluation import (
        EvaluateRequest, EvaluateResponse, EvaluatorInfo,
        EvaluateBatchRequest, EvaluateBatchResponse,
        get_eval_settings, list_evaluators,
        run_evaluation, run_batch_evaluation,
        format_html_report, format_batch_html_report,
    )
"""
from .models import (
    EvaluateBatchItemResult,
    EvaluateBatchRequest,
    EvaluateBatchResponse,
    EvaluateBatchSummary,
    EvaluateRequest,
    EvaluateResponse,
    EvaluateSummary,
    EvaluatorInfo,
    EvaluatorResult,
)
from .runner import (
    format_batch_html_report,
    format_html_report,
    list_evaluators,
    run_batch_evaluation,
    run_evaluation,
)
from .settings import EvaluationSettings, get_eval_settings

__all__ = [
    "EvaluateBatchItemResult",
    "EvaluateBatchRequest",
    "EvaluateBatchResponse",
    "EvaluateBatchSummary",
    "EvaluateRequest",
    "EvaluateResponse",
    "EvaluateSummary",
    "EvaluatorInfo",
    "EvaluatorResult",
    "EvaluationSettings",
    "get_eval_settings",
    "list_evaluators",
    "run_evaluation",
    "run_batch_evaluation",
    "format_html_report",
    "format_batch_html_report",
]
