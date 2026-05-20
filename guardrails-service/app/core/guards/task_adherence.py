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
# Money / amount patterns that count as banking signal regardless of
# whether a sentence repeats the literal words "transaction" /
# "account". A bank assistant listing specific transactions will say
# things like "on May 15 you spent $42.00 at Starbucks" - the dollar
# amount is itself an in-scope signal.
_MONEY_RE = re.compile(
    r"(?:\$\s?\d|\d+\s?(?:usd|eur|gbp|inr|cad|aud)\b|\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b)",
    re.IGNORECASE,
)


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


# Tiny English stopword set used to extract content words from a free-text
# task definition. Kept inline so the guard has no external NLP deps.
_STOPWORDS = frozenset(
    """
    a an the of and or but if then else for to in on at by with from as is are was were be been being
    it its this that these those i you he she we they them us our your their my me his her him hers ours yours theirs
    do does did done doing have has had having will would shall should can could may might must
    not no nor only own same so than too very s t just don now also about across after before
    into onto over under up down out off again further here there when where why how all any both each
    few more most other some such only own same any all such which who whom whose what
    it's don't won't can't isn't aren't doesn't didn't hasn't haven't hadn't wouldn't shouldn't couldn't
    e.g eg etc i.e ie vs via per
    """.split()
)


def _keywords_from_task_definition(task_def: str) -> list[str]:
    """Extract simple content-word keywords from a free-text task description.

    Tokenises on word boundaries, lowercases, drops short tokens and a
    small English stopword set, and de-duplicates while preserving
    first-seen order. Also adds naive singular/plural variants so a
    description that says "transfers / loans / cards" also matches
    queries that use the singular form. Used so policy authors can hand
    the local ``task-adherence`` guard the same free-text scope they
    pass to the Azure managed ``azure-task-adherence`` guard.
    """
    seen: dict[str, None] = {}

    def _add(tok: str) -> None:
        if len(tok) < 3 or tok in _STOPWORDS:
            return
        seen.setdefault(tok, None)

    for raw in _WORD_RE.findall(task_def or ""):
        tok = raw.lower()
        _add(tok)
        # Naive singular form: "transfers" -> "transfer", "loans" -> "loan",
        # "branches" -> "branche" (harmless; we also add the "es"-stripped
        # form), "mortgages" -> "mortgage".
        if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
            _add(tok[:-1])
        if len(tok) > 4 and tok.endswith("es"):
            _add(tok[:-2])
        # Naive plural form so a description using the singular still
        # matches plural queries: "card" -> "cards", "loan" -> "loans".
        if len(tok) >= 3 and not tok.endswith("s"):
            _add(tok + "s")
    return list(seen.keys())


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
        # Free-text task description (same value policy authors pass to
        # the Azure managed `azure-task-adherence` guard). When supplied
        # and no explicit `in_scope_keywords` list is given, the local
        # keyword engine derives its in-scope vocabulary from it so the
        # two guards stay in sync from a single policy anchor.
        self.task_definition: str = str(config.get("task_definition", "")).strip()
        explicit_in_scope = config.get("in_scope_keywords")
        if explicit_in_scope is not None:
            self.in_scope_keywords: list[str] = list(explicit_in_scope)
        elif self.task_definition:
            self.in_scope_keywords = _keywords_from_task_definition(
                self.task_definition
            )
        else:
            self.in_scope_keywords = list(DEFAULT_IN_SCOPE)
        self.out_of_scope_keywords: list[str] = list(
            config.get("out_of_scope_keywords", DEFAULT_OUT_OF_SCOPE)
        )
        # Strict in-scope mode: any sentence that does NOT match an
        # in-scope keyword is treated as out-of-scope (no need to
        # enumerate every off-topic term). The out_of_scope_keywords
        # list is ignored when this is on.
        self.strict_in_scope: bool = bool(config.get("strict_in_scope", False))
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
        counts_note = (
            f"in_scope_sentences={in_count} out_of_scope_sentences={out_count}"
        )

        # Hard rule: no in-scope signal anywhere in the reply AND at
        # least `min_out_of_scope_sentences` off-scope sentences. This is
        # the ONLY BLOCK path in strict_in_scope mode - if the reply
        # contains at least one banking sentence we trust it, even when
        # other sentences (greetings, amounts, merchant names) don't
        # repeat banking vocabulary verbatim.
        if in_count == 0 and out_count >= self.min_out_of_scope_sentences:
            return self._block(
                text,
                reasons=[
                    f"{out_count} out-of-scope sentences and no in-scope signal ({counts_note})",
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

        # In strict_in_scope mode we ONLY use the zero-in-scope rule
        # above. Score-based BLOCK / SANITIZE are intentionally skipped
        # so legitimate banking answers that quote dates / amounts /
        # merchant names per sentence don't get scored as off-scope.
        if self.strict_in_scope:
            return self._allow(
                text,
                score=score,
                metadata={
                    "engine": self.engine,
                    "strict_in_scope": True,
                    "in_scope_sentences": in_count,
                    "out_of_scope_sentences": out_count,
                },
            )

        if score < self.block_threshold:
            return self._block(
                text,
                reasons=[
                    f"task-adherence score {score:.2f} < block_threshold {self.block_threshold:.2f} ({counts_note})",
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
                    f"task-adherence score {score:.2f} < warn_threshold {self.warn_threshold:.2f} ({counts_note})",
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
            has_in = _has_any(sent, self.in_scope_keywords) or bool(
                _MONEY_RE.search(sent)
            )
            if has_in:
                in_count += 1
                continue
            if self.strict_in_scope:
                # No in-scope term -> off-scope by default.
                out_count += 1
                out_samples.append(sent)
                continue
            if _has_any(sent, self.out_of_scope_keywords):
                out_count += 1
                out_samples.append(sent)
        return in_count, out_count, out_samples


register_guard("task-adherence", lambda cfg: TaskAdherenceGuard(**cfg))
