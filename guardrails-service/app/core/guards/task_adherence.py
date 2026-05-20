"""task-adherence: keep the assistant's reply inside the allowed task scope.

WHY THIS GUARD EXISTS
---------------------
Azure AI Foundry ships a ``TaskAdherenceEvaluator`` in
``azure-ai-evaluation``, but it is an **evaluation-time** metric (preview)
and there is no GA realtime content filter for task adherence. This
guard enforces task adherence at runtime as an output-stage check,
complementing the input-stage ``banking-relevance`` guard.

HOW IT DECIDES
--------------
Given the assistant's reply, score how much of it stays within the
declared task scope (banking-assistant by default). The reply is split
into sentences; each sentence is classified as in-scope, out-of-scope,
or neutral by lexical signals.

Engines:
    * ``"keyword"`` (default, offline, deterministic)
        - In-scope: at least one ``in_scope_keywords`` term present.
        - Out-of-scope: at least one ``out_of_scope_keywords`` term and
          no in-scope term.
        - Neutral: neither.
        Reply score = in_scope / max(1, in_scope + out_of_scope).
    * ``"azure-eval"`` (placeholder)
        Reserved for ``azure-ai-evaluation.TaskAdherenceEvaluator``.
        Falls back to keyword until wired.
    * ``"llm-judge"`` (placeholder)
        Reserved for a small judge prompt. Falls back to keyword.

DECISIONS
---------
    * Reply too short: ALLOW (nothing to adhere to).
    * No in-scope and >= ``min_out_of_scope_sentences`` out-of-scope
      sentences: BLOCK.
    * Score < ``block_threshold``: BLOCK.
    * Score < ``warn_threshold``: SANITIZE (append a polite scope
      reminder, do not rewrite the content).
    * Otherwise: ALLOW.

CONFIGURATION KEYS (set via ``GUARD_TASK_ADHERENCE_CONFIG``, JSON object)
------------------------------------------------------------------------
    engine                      "keyword" | "azure-eval" | "llm-judge"
    in_scope_keywords           list[str], default = banking vocab
    out_of_scope_keywords       list[str], default = common off-topic
                                terms (poems, recipes, stocks, ...)
    block_threshold             float 0..1, default 0.34
    warn_threshold              float 0..1, default 0.6
    min_out_of_scope_sentences  int, default 2
    min_length                  int, default 60
    block_message               str, refusal text on BLOCK
    reminder_suffix             str, appended on SANITIZE

EXAMPLE
-------
    GUARD_TASK_ADHERENCE_ENABLED=true
    GUARD_TASK_ADHERENCE_CONFIG='{
        "engine": "keyword",
        "out_of_scope_keywords": ["poem","recipe","stock tip","weather"]
    }'
"""
from __future__ import annotations

import re
from typing import Any

from ..base import Guard, GuardCheckResult, GuardStage
from ..registry import register_guard

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'-]+")
_SENT_RE = re.compile(r"(?<=[.!?])\s+")


DEFAULT_IN_SCOPE = [
    "account", "balance", "transaction", "transfer", "wire",
    "card", "credit", "debit", "atm", "branch",
    "loan", "mortgage", "interest", "apr",
    "deposit", "withdraw", "withdrawal", "statement",
    "payment", "bill", "billing", "savings", "checking",
    "currency", "fx", "fee", "fees", "fraud", "bank", "banking",
]

DEFAULT_OUT_OF_SCOPE = [
    "poem", "poetry", "haiku", "lyrics", "song",
    "recipe", "cook", "cooking",
    "stock tip", "buy stock", "sell stock", "crypto signal", "moon",
    "horoscope", "astrology", "tarot",
    "weather forecast",
    "medical advice", "diagnose", "prescription",
    "legal advice", "lawsuit",
    "joke", "story", "novel",
]


def _has_any(text: str, needles: list[str]) -> bool:
    low = text.lower()
    return any(n.lower() in low for n in needles)


def _sentences(text: str) -> list[str]:
    parts = [s.strip() for s in _SENT_RE.split(text.strip()) if s.strip()]
    return parts or ([text.strip()] if text.strip() else [])


class TaskAdherenceGuard(Guard):
    name = "task-adherence"
    stage = GuardStage.OUTPUT
    description = (
        "Keep the assistant's reply within the declared task scope. "
        "Runtime complement to Foundry's TaskAdherenceEvaluator (preview)."
    )

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)
        self.engine: str = str(config.get("engine", "keyword")).lower()
        self.in_scope_keywords: list[str] = list(
            config.get("in_scope_keywords", DEFAULT_IN_SCOPE)
        )
        self.out_of_scope_keywords: list[str] = list(
            config.get("out_of_scope_keywords", DEFAULT_OUT_OF_SCOPE)
        )
        self.block_threshold: float = float(config.get("block_threshold", 0.34))
        self.warn_threshold: float = float(config.get("warn_threshold", 0.6))
        self.min_out_of_scope_sentences: int = int(
            config.get("min_out_of_scope_sentences", 2)
        )
        self.min_length: int = int(config.get("min_length", 60))
        self.block_message: str = str(
            config.get(
                "block_message",
                "Let's stay focused on banking - I'm not able to help with that here.",
            )
        )
        self.reminder_suffix: str = str(
            config.get(
                "reminder_suffix",
                "\n\n_(I can only help with banking topics like accounts, transfers, cards, and loans.)_",
            )
        )

    async def check(self, text: str, *, context: dict[str, Any] | None = None) -> GuardCheckResult:
        reply = (text or "").strip()
        if len(reply) < self.min_length:
            return self._allow(text, metadata={"skipped": "too-short"})

        # Future: branch on self.engine; placeholders fall back to keyword.
        in_count, out_count, samples = self._score_keyword(reply)

        total = in_count + out_count
        score = (in_count / total) if total else 1.0

        # Hard rule: many out-of-scope sentences with zero in-scope signal.
        if in_count == 0 and out_count >= self.min_out_of_scope_sentences:
            return self._block(
                text,
                reasons=[
                    f"{out_count} out-of-scope sentences and no in-scope signal",
                    self.block_message,
                ],
                categories=["rai.task-adherence.off-scope"],
                score=score,
                metadata={
                    "engine": self.engine,
                    "in_scope_sentences": in_count,
                    "out_of_scope_sentences": out_count,
                    "samples": samples[:5],
                },
            )

        if score < self.block_threshold:
            return self._block(
                text,
                reasons=[
                    f"task-adherence score {score:.2f} < block_threshold {self.block_threshold:.2f}",
                    self.block_message,
                ],
                categories=["rai.task-adherence.low"],
                score=score,
                metadata={
                    "engine": self.engine,
                    "in_scope_sentences": in_count,
                    "out_of_scope_sentences": out_count,
                    "samples": samples[:5],
                },
            )

        if score < self.warn_threshold:
            return self._sanitize(
                reply + self.reminder_suffix,
                reasons=[
                    f"task-adherence score {score:.2f} < warn_threshold {self.warn_threshold:.2f}",
                ],
                categories=["rai.task-adherence.weak"],
                score=score,
                metadata={
                    "engine": self.engine,
                    "in_scope_sentences": in_count,
                    "out_of_scope_sentences": out_count,
                },
            )

        return self._allow(
            text,
            score=score,
            metadata={
                "engine": self.engine,
                "in_scope_sentences": in_count,
                "out_of_scope_sentences": out_count,
            },
        )

    # ------------------------------------------------------------------
    # Keyword engine
    # ------------------------------------------------------------------

    def _score_keyword(self, reply: str) -> tuple[int, int, list[str]]:
        in_count = 0
        out_count = 0
        out_samples: list[str] = []
        for sent in _sentences(reply):
            has_in = _has_any(sent, self.in_scope_keywords)
            has_out = _has_any(sent, self.out_of_scope_keywords)
            if has_in:
                in_count += 1
            elif has_out:
                out_count += 1
                out_samples.append(sent)
        return in_count, out_count, out_samples


register_guard("task-adherence", lambda cfg: TaskAdherenceGuard(**cfg))
