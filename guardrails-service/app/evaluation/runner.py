"""Evaluation runner and HTML report generator.

Public API:
  run_evaluation(request, settings)         -> EvaluateResponse
  run_batch_evaluation(request, settings)   -> EvaluateBatchResponse
  list_evaluators(stage, settings)          -> list[EvaluatorInfo]
  format_html_report(response)              -> str  (standalone HTML page)
  format_batch_html_report(response)        -> str  (multi-item HTML page)
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from ..core.observability import obs_log
from .evaluators import EVALUATOR_REGISTRY, _ALL_WRAPPER_CLASSES, get_evaluators_for_stage
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
from .settings import EvaluationSettings


# ---------------------------------------------------------------------------
# run_evaluation
# ---------------------------------------------------------------------------

async def run_evaluation(
    request: EvaluateRequest,
    settings: EvaluationSettings,
) -> EvaluateResponse:
    """Run all applicable evaluators concurrently and return an EvaluateResponse."""
    t0 = time.perf_counter()
    stage = request.stage

    obs_log(
        "evaluation.start",
        stage=stage,
        query_len=len(request.query),
        response_len=len(request.response),
        requested_evaluators=request.evaluators,
    )

    evaluators = get_evaluators_for_stage(stage, settings, requested=request.evaluators)
    if not evaluators:
        obs_log("evaluation.no_evaluators", stage=stage, level="warning")

    # Run all evaluators concurrently
    tasks = [
        e.evaluate(
            query=request.query,
            response=request.response,
            context=request.context,
            ground_truth=request.ground_truth,
        )
        for e in evaluators
    ]
    results: list[EvaluatorResult] = list(
        await asyncio.gather(*tasks, return_exceptions=False)
    )

    duration_ms = (time.perf_counter() - t0) * 1000

    # ---- Aggregate ----
    failed = [r for r in results if r.status == "failed"]
    skipped = [r for r in results if r.status == "skipped"]
    errored = [r for r in results if r.status == "error"]
    passed = [r for r in results if r.status == "passed"]

    safety_results = [r for r in results if r.category == "safety"]
    quality_results = [r for r in results if r.category == "quality"]
    safety_pass = all(r.status != "failed" for r in safety_results)
    quality_pass = all(r.status != "failed" for r in quality_results)
    overall_pass = safety_pass and quality_pass

    scored_quality = [
        r.score
        for r in quality_results
        if r.score is not None and r.status not in ("skipped", "error")
    ]
    avg_quality_score: float | None = (
        round(sum(scored_quality) / len(scored_quality), 2) if scored_quality else None
    )

    by_category: dict[str, dict[str, int]] = {}
    for cat in ("safety", "quality", "nlp"):
        cat_results = [r for r in results if r.category == cat]
        by_category[cat] = {
            "total": len(cat_results),
            "passed": sum(1 for r in cat_results if r.status == "passed"),
            "failed": sum(1 for r in cat_results if r.status == "failed"),
            "skipped": sum(1 for r in cat_results if r.status == "skipped"),
            "error": sum(1 for r in cat_results if r.status == "error"),
        }

    summary = EvaluateSummary(
        total=len(results),
        passed=len(passed),
        failed=len(failed),
        skipped=len(skipped),
        error=len(errored),
        safety_pass=safety_pass,
        quality_pass=quality_pass,
        avg_quality_score=avg_quality_score,
        evaluators_run=[r.name for r in results if r.status not in ("skipped",)],
        by_category=by_category,
    )

    obs_log(
        "evaluation.complete",
        stage=stage,
        overall_pass=overall_pass,
        total=len(results),
        failed=len(failed),
        skipped=len(skipped),
        duration_ms=round(duration_ms, 1),
    )

    return EvaluateResponse(
        stage=stage,
        query=request.query,
        response=request.response,
        overall_pass=overall_pass,
        safety_pass=safety_pass,
        quality_pass=quality_pass,
        summary=summary,
        evaluator_results=results,
        failed_evaluators=[r.name for r in failed],
        skipped_evaluators=[r.name for r in skipped],
        duration_ms=duration_ms,
        metadata=request.metadata,
    )


# ---------------------------------------------------------------------------
# list_evaluators
# ---------------------------------------------------------------------------

def list_evaluators(
    stage: str | None,
    settings: EvaluationSettings,
) -> list[EvaluatorInfo]:
    """Describe all evaluators, optionally filtered to a specific stage."""
    infos: list[EvaluatorInfo] = []
    for cls in _ALL_WRAPPER_CLASSES:
        if stage is not None:
            canonical = stage.replace("llm_input", "input").replace("llm_output", "output")
            if canonical not in cls.stages:
                continue
        wrapper = cls(settings)
        infos.append(
            EvaluatorInfo(
                name=cls.name,
                category=cls.category,  # type: ignore[arg-type]
                description=cls.description,
                stages=sorted(cls.stages),
                requires=list(cls.requires),
                available=wrapper.is_available(),
            )
        )
    return infos


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

_STATUS_STYLE: dict[str, str] = {
    "passed": "background:#d1fae5;color:#065f46;font-weight:600",
    "failed": "background:#fee2e2;color:#991b1b;font-weight:600",
    "skipped": "background:#f3f4f6;color:#6b7280",
    "error": "background:#fef3c7;color:#92400e;font-weight:600",
}

_CATEGORY_BADGE: dict[str, str] = {
    "safety": "background:#dbeafe;color:#1e40af",
    "quality": "background:#ede9fe;color:#5b21b6",
    "nlp": "background:#fef9c3;color:#713f12",
}


def _status_icon(status: str) -> str:
    return {"passed": "✅", "failed": "❌", "skipped": "⏭", "error": "⚠️"}.get(status, "•")


def _row(r: EvaluatorResult) -> str:
    st_style = _STATUS_STYLE.get(r.status, "")
    cat_style = _CATEGORY_BADGE.get(r.category, "")
    score_str = f"{r.score:.3f}" if r.score is not None else "—"
    label_str = r.label or "—"
    reason_str = (r.reason or r.error or "")[:200]
    return (
        f"<tr>"
        f"<td style='padding:8px 12px;font-weight:600'>{r.name}</td>"
        f"<td style='padding:8px 12px'>"
        f"  <span style='padding:2px 8px;border-radius:4px;font-size:12px;{cat_style}'>{r.category}</span>"
        f"</td>"
        f"<td style='padding:8px 12px'>"
        f"  <span style='padding:3px 10px;border-radius:6px;{st_style}'>{_status_icon(r.status)} {r.status}</span>"
        f"</td>"
        f"<td style='padding:8px 12px;text-align:right'>{score_str}</td>"
        f"<td style='padding:8px 12px'>{label_str}</td>"
        f"<td style='padding:8px 12px;font-size:13px;color:#374151;max-width:380px'>{reason_str}</td>"
        f"<td style='padding:8px 12px;text-align:right;font-size:12px;color:#6b7280'>{r.duration_ms:.0f} ms</td>"
        f"</tr>"
    )


def format_html_report(resp: EvaluateResponse) -> str:
    """Return a self-contained HTML page visualising the evaluation results."""
    overall_color = "#065f46" if resp.overall_pass else "#991b1b"
    overall_bg = "#d1fae5" if resp.overall_pass else "#fee2e2"
    overall_label = "✅ OVERALL PASS" if resp.overall_pass else "❌ OVERALL FAIL"

    s = resp.summary
    rows_html = "".join(_row(r) for r in resp.evaluator_results)

    # Summary cards
    def card(title: str, value: Any, bg: str = "#f9fafb") -> str:
        return (
            f"<div style='background:{bg};border-radius:8px;padding:12px 20px;"
            f"text-align:center;min-width:100px'>"
            f"<div style='font-size:24px;font-weight:700'>{value}</div>"
            f"<div style='font-size:12px;color:#6b7280;margin-top:2px'>{title}</div>"
            f"</div>"
        )

    aq = f"{s.avg_quality_score:.2f} / 5" if s.avg_quality_score is not None else "—"
    cards = (
        card("Total", s.total)
        + card("Passed", s.passed, "#d1fae5")
        + card("Failed", s.failed, "#fee2e2" if s.failed else "#f9fafb")
        + card("Skipped", s.skipped)
        + card("Errors", s.error, "#fef3c7" if s.error else "#f9fafb")
        + card("Avg Quality", aq, "#ede9fe")
    )

    # Category summary rows
    cat_rows = ""
    for cat, counts in s.by_category.items():
        if counts["total"] == 0:
            continue
        cat_style = _CATEGORY_BADGE.get(cat, "")
        pass_pct = (
            round(counts["passed"] / counts["total"] * 100) if counts["total"] else 0
        )
        cat_rows += (
            f"<tr>"
            f"<td style='padding:6px 12px'>"
            f"<span style='padding:2px 8px;border-radius:4px;font-size:12px;{cat_style}'>{cat}</span></td>"
            f"<td style='padding:6px 12px;text-align:center'>{counts['total']}</td>"
            f"<td style='padding:6px 12px;text-align:center;color:#065f46'>{counts['passed']}</td>"
            f"<td style='padding:6px 12px;text-align:center;color:#991b1b'>{counts['failed']}</td>"
            f"<td style='padding:6px 12px;text-align:center'>{counts['skipped']}</td>"
            f"<td style='padding:6px 12px;text-align:center'>{pass_pct}%</td>"
            f"</tr>"
        )

    query_display = resp.query[:300].replace("<", "&lt;").replace(">", "&gt;")
    response_display = resp.response[:500].replace("<", "&lt;").replace(">", "&gt;")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Guardrails Evaluation Report — {resp.stage}</title>
<style>
  body {{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:0;padding:24px;background:#f1f5f9;color:#111827}}
  h1 {{margin:0 0 4px;font-size:22px}}
  h2 {{font-size:16px;margin:20px 0 8px;color:#374151}}
  table {{border-collapse:collapse;width:100%;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1)}}
  th {{background:#f8fafc;padding:10px 12px;text-align:left;font-size:13px;color:#6b7280;border-bottom:1px solid #e5e7eb}}
  tr:nth-child(even) td {{background:#fafafa}}
  tr:hover td {{background:#f0f9ff}}
  td {{border-bottom:1px solid #f3f4f6;vertical-align:top}}
  .badge {{display:inline-block;padding:3px 10px;border-radius:6px;font-size:14px;font-weight:600}}
  pre {{background:#f8fafc;padding:12px;border-radius:6px;font-size:13px;overflow-x:auto;white-space:pre-wrap;word-break:break-word}}
</style>
</head>
<body>

<div style="max-width:1200px;margin:0 auto">

  <div style="display:flex;align-items:center;gap:16px;margin-bottom:20px">
    <div>
      <h1>Guardrails Evaluation Report</h1>
      <div style="color:#6b7280;font-size:14px">Stage: <strong>{resp.stage}</strong>
        &nbsp;|&nbsp; {resp.summary.total} evaluator(s) &nbsp;|&nbsp;
        {resp.duration_ms:.0f} ms total
      </div>
    </div>
    <div style="margin-left:auto">
      <span class="badge" style="background:{overall_bg};color:{overall_color};font-size:16px">
        {overall_label}
      </span>
    </div>
  </div>

  <!-- Safety / Quality badges -->
  <div style="display:flex;gap:12px;margin-bottom:20px">
    <span class="badge" style="background:{'#d1fae5' if resp.safety_pass else '#fee2e2'};color:{'#065f46' if resp.safety_pass else '#991b1b'}">
      {'✅' if resp.safety_pass else '❌'} Safety
    </span>
    <span class="badge" style="background:{'#d1fae5' if resp.quality_pass else '#fee2e2'};color:{'#065f46' if resp.quality_pass else '#991b1b'}">
      {'✅' if resp.quality_pass else '❌'} Quality
    </span>
  </div>

  <!-- Summary cards -->
  <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:24px">
    {cards}
  </div>

  <!-- Category breakdown -->
  <h2>Results by Category</h2>
  <table style="margin-bottom:24px">
    <thead><tr>
      <th>Category</th><th style="text-align:center">Total</th>
      <th style="text-align:center">Passed</th><th style="text-align:center">Failed</th>
      <th style="text-align:center">Skipped</th><th style="text-align:center">Pass %</th>
    </tr></thead>
    <tbody>{cat_rows}</tbody>
  </table>

  <!-- Per-evaluator results -->
  <h2>Evaluator Results</h2>
  <table style="margin-bottom:24px">
    <thead><tr>
      <th>Evaluator</th><th>Category</th><th>Status</th>
      <th style="text-align:right">Score</th><th>Label</th>
      <th>Reason / Detail</th><th style="text-align:right">Time</th>
    </tr></thead>
    <tbody>{rows_html}</tbody>
  </table>

  <!-- Query / Response -->
  <h2>Input</h2>
  <div style="margin-bottom:8px"><strong style="font-size:13px;color:#6b7280">QUERY</strong>
    <pre>{query_display}</pre>
  </div>
  <div><strong style="font-size:13px;color:#6b7280">RESPONSE</strong>
    <pre>{response_display}</pre>
  </div>

</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# run_batch_evaluation
# ---------------------------------------------------------------------------

async def run_batch_evaluation(
    batch_req: EvaluateBatchRequest,
    settings: EvaluationSettings,
) -> EvaluateBatchResponse:
    """Run multiple evaluation requests concurrently and return a batch response.

    Each item in ``batch_req.items`` is evaluated independently.  If the item
    has no ``evaluators`` filter, the batch-level ``batch_req.evaluators`` is
    used as the default (None = all available).
    """
    t0 = time.perf_counter()

    # Apply batch-level evaluator default to items that don't specify their own
    resolved: list[EvaluateRequest] = []
    for item in batch_req.items:
        if item.evaluators is None and batch_req.evaluators is not None:
            resolved.append(item.model_copy(update={"evaluators": batch_req.evaluators}))
        else:
            resolved.append(item)

    obs_log(
        "evaluation.batch.start",
        item_count=len(resolved),
    )

    # Run all items concurrently
    results: list[EvaluateResponse] = list(
        await asyncio.gather(
            *[run_evaluation(req, settings) for req in resolved],
            return_exceptions=False,
        )
    )

    duration_ms = (time.perf_counter() - t0) * 1000

    items_passed = sum(1 for r in results if r.overall_pass)
    items_failed = len(results) - items_passed
    total_evaluators_run = sum(r.summary.total for r in results)
    total_errors = sum(r.summary.error for r in results)

    summary = EvaluateBatchSummary(
        total_items=len(results),
        items_passed=items_passed,
        items_failed=items_failed,
        total_evaluators_run=total_evaluators_run,
        total_evaluator_errors=total_errors,
        overall_pass=items_failed == 0,
    )

    obs_log(
        "evaluation.batch.complete",
        total_items=len(results),
        items_passed=items_passed,
        items_failed=items_failed,
        duration_ms=round(duration_ms, 1),
    )

    return EvaluateBatchResponse(
        items=[
            EvaluateBatchItemResult(index=i, result=r)
            for i, r in enumerate(results)
        ],
        summary=summary,
        duration_ms=duration_ms,
    )


# ---------------------------------------------------------------------------
# Batch HTML report
# ---------------------------------------------------------------------------

def format_batch_html_report(batch: EvaluateBatchResponse) -> str:
    """Return a self-contained HTML page visualising all batch evaluation results."""
    s = batch.summary
    overall_color = "#065f46" if s.overall_pass else "#991b1b"
    overall_bg = "#d1fae5" if s.overall_pass else "#fee2e2"
    overall_label = "✅ ALL ITEMS PASS" if s.overall_pass else f"❌ {s.items_failed} ITEM(S) FAILED"

    def card(title: str, value: Any, bg: str = "#f9fafb") -> str:
        return (
            f"<div style='background:{bg};border-radius:8px;padding:12px 20px;"
            f"text-align:center;min-width:90px'>"
            f"<div style='font-size:24px;font-weight:700'>{value}</div>"
            f"<div style='font-size:12px;color:#6b7280;margin-top:2px'>{title}</div>"
            f"</div>"
        )

    summary_cards = (
        card("Total Items", s.total_items)
        + card("Items Passed", s.items_passed, "#d1fae5")
        + card("Items Failed", s.items_failed, "#fee2e2" if s.items_failed else "#f9fafb")
        + card("Evaluators Run", s.total_evaluators_run)
        + card("Eval Errors", s.total_evaluator_errors, "#fef3c7" if s.total_evaluator_errors else "#f9fafb")
        + card("Total Time", f"{batch.duration_ms:.0f} ms")
    )

    # Per-item sections
    item_sections = ""
    for item in batch.items:
        resp = item.result
        item_color = "#065f46" if resp.overall_pass else "#991b1b"
        item_bg = "#d1fae5" if resp.overall_pass else "#fee2e2"
        item_label = "✅ PASS" if resp.overall_pass else "❌ FAIL"
        rows_html = "".join(_row(r) for r in resp.evaluator_results)
        q_display = resp.query[:200].replace("<", "&lt;").replace(">", "&gt;")
        r_display = resp.response[:300].replace("<", "&lt;").replace(">", "&gt;")

        item_sections += f"""
  <div style="background:#fff;border-radius:10px;padding:20px;margin-bottom:20px;
              box-shadow:0 1px 3px rgba(0,0,0,.1);border-left:4px solid {item_color}">
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px">
      <div>
        <span style="font-size:16px;font-weight:700">Item #{item.index + 1}</span>
        &nbsp;&nbsp;<span style="font-size:13px;color:#6b7280">Stage: <strong>{resp.stage}</strong>
          &nbsp;|&nbsp; {resp.summary.total} evaluator(s) &nbsp;|&nbsp; {resp.duration_ms:.0f} ms</span>
      </div>
      <div style="margin-left:auto">
        <span style="padding:3px 12px;border-radius:6px;font-weight:600;font-size:14px;
                     background:{item_bg};color:{item_color}">{item_label}</span>
      </div>
    </div>
    <div style="display:flex;gap:8px;margin-bottom:10px;font-size:12px">
      <strong style="color:#6b7280">Q:</strong>
      <span style="color:#374151">{q_display}</span>
    </div>
    <div style="display:flex;gap:8px;margin-bottom:14px;font-size:12px">
      <strong style="color:#6b7280">A:</strong>
      <span style="color:#374151">{r_display}</span>
    </div>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead><tr style="background:#f8fafc">
        <th style="padding:8px 12px;text-align:left;color:#6b7280">Evaluator</th>
        <th style="padding:8px 12px;text-align:left;color:#6b7280">Category</th>
        <th style="padding:8px 12px;text-align:left;color:#6b7280">Status</th>
        <th style="padding:8px 12px;text-align:right;color:#6b7280">Score</th>
        <th style="padding:8px 12px;text-align:left;color:#6b7280">Label</th>
        <th style="padding:8px 12px;text-align:left;color:#6b7280">Reason / Detail</th>
        <th style="padding:8px 12px;text-align:right;color:#6b7280">Time</th>
      </tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Guardrails Batch Evaluation Report</title>
<style>
  body {{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:0;padding:24px;background:#f1f5f9;color:#111827}}
  h1 {{margin:0 0 4px;font-size:22px}}
  tr:nth-child(even) td {{background:#fafafa}}
  tr:hover td {{background:#f0f9ff}}
  td {{border-bottom:1px solid #f3f4f6;vertical-align:top}}
</style>
</head>
<body>
<div style="max-width:1200px;margin:0 auto">

  <div style="display:flex;align-items:center;gap:16px;margin-bottom:20px">
    <div>
      <h1>Guardrails Batch Evaluation Report</h1>
      <div style="color:#6b7280;font-size:14px">{s.total_items} item(s) &nbsp;|&nbsp; {batch.duration_ms:.0f} ms total</div>
    </div>
    <div style="margin-left:auto">
      <span style="padding:4px 16px;border-radius:8px;font-weight:700;font-size:16px;
                   background:{overall_bg};color:{overall_color}">{overall_label}</span>
    </div>
  </div>

  <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:28px">
    {summary_cards}
  </div>

  {item_sections}

</div>
</body>
</html>"""
