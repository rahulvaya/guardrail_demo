"""topic-relevance: REFERENCE IMPLEMENTATION OF A CUSTOM GUARDRAIL.

Refuse inputs that are clearly off-topic for the consumer's domain
(e.g. a customer-support bot getting "write me a poem about cats").
This protects LLM budget from abuse and keeps the assistant on-mission.

This guard is intentionally domain-NEUTRAL: it has no built-in
vocabulary. Each consumer supplies their own `keywords` list via
policy config. If `keywords` is empty, the guard short-circuits and
allows everything (no-op).

The companion authoring guide lives in the service README under
"Creating custom guards".

HOW IT DECIDES
--------------
Two-stage scoring:
    1. Allow-list: if the input contains any of the configured
       keywords, the guard ALLOWS immediately - cheap and
       deterministic.
    2. Otherwise: compute a *match-ratio* = matched_terms /
       total_meaningful_words. If that ratio is below `min_ratio` AND
       the input is longer than `min_length` characters, BLOCK with a
       polite refusal reason.

CONFIGURATION KEYS (policy YAML or `GUARD_TOPIC_RELEVANCE_CONFIG` JSON)
----------------------------------------------------------------------
    keywords        list[str]   domain vocabulary; REQUIRED to enable
    min_ratio       float       0..1, default 0.05
    min_length      int         skip inputs shorter than this (chars)
    refusal_message str         shown to the user when blocked

EXAMPLE - retail banking
------------------------
    - topic-relevance:
        enabled: true
        keywords: ["account","balance","loan","card","atm","transfer"]
        min_ratio: 0.05
        refusal_message: "I can only help with banking questions."

EXAMPLE - HR helpdesk
---------------------
    - topic-relevance:
        enabled: true
        keywords: ["leave","pto","payroll","benefits","manager","timesheet"]
        refusal_message: "I can only help with HR questions."

EXTENDING
---------
For higher recall swap the keyword check for an LLM-based judge by
overriding `_score()`. The pipeline contract (return `GuardCheckResult`)
stays the same - that's why this is a single-file change.

CHECKLIST FOR CUSTOM GUARDS
---------------------------
    [ ] Subclass `Guard`.
    [ ] Set `name`, `stage`, `description` class attributes.
    [ ] Accept config via `__init__(**config)` and store on `self.config`.
    [ ] Implement async `check(text, *, context)` returning a
        `GuardCheckResult`. Use `self._allow / _sanitize / _block`.
    [ ] Never raise; the pipeline treats raises as ALLOW with a warning.
    [ ] Register at module load: `register_guard("<name>", factory)`.
    [ ] Import the module in `guards/__init__.py`.
    [ ] Document config keys in `.env.example` and the README.
    [ ] Add a unit test under `tests/`.
"""
from __future__ import annotations

import re
from typing import Any

from ..base import Guard, GuardCheckResult, GuardStage
from ..registry import register_guard
from .task_adherence import _keywords_from_task_definition

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'-]+")


class TopicRelevanceGuard(Guard):
    name = "topic-relevance"
    stage = GuardStage.INPUT
    description = "Refuse off-topic inputs (anything outside the configured domain)."

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)
        explicit = config.get("keywords")
        # DRY with `task-adherence`: when no explicit `keywords:` list
        # is given but a free-text `task_definition:` is, derive the
        # in-scope vocabulary from it (same anchor both guards share).
        self.task_definition: str = str(config.get("task_definition", "")).strip()
        if explicit:
            self.keywords: set[str] = {w.lower() for w in explicit}
        elif self.task_definition:
            self.keywords = {
                w.lower() for w in _keywords_from_task_definition(self.task_definition)
            }
        else:
            self.keywords = set()
        self.min_ratio: float = float(config.get("min_ratio", 0.05))
        self.min_length: int = int(config.get("min_length", 3))
        self.refusal_message: str = str(
            config.get(
                "refusal_message",
                "I can only help with topics in this assistant's configured scope.",
            )
        )

    async def check(self, text: str, *, context: dict[str, Any] | None = None) -> GuardCheckResult:
        # No keywords configured -> guard is a no-op (domain neutral default).
        if not self.keywords:
            return self._allow(text, metadata={"skipped": "no-keywords-configured"})

        if len(text.strip()) < self.min_length:
            return self._allow(text, metadata={"skipped": "too-short"})

        words = [w.lower() for w in _WORD_RE.findall(text)]
        if not words:
            return self._allow(text, metadata={"skipped": "no-words"})

        matched = [w for w in words if w in self.keywords]
        ratio = len(matched) / len(words)
        if matched:
            return self._allow(text, score=ratio, metadata={"matched": matched, "ratio": ratio})

        if ratio < self.min_ratio:
            return self._block(
                text,
                reasons=[
                    f"off-topic: ratio={ratio:.2f} < min_ratio={self.min_ratio}",
                    self.refusal_message,
                ],
                categories=["policy.off-topic"],
                score=ratio,
                metadata={"matched": matched, "ratio": ratio, "word_count": len(words)},
            )
        return self._allow(text, score=ratio)


# Canonical name. (The legacy "banking-relevance" alias was removed
# once all in-tree policies migrated to "topic-relevance".)
register_guard("topic-relevance", lambda cfg: TopicRelevanceGuard(**cfg))
