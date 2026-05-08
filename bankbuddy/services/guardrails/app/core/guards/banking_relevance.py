"""banking-relevance: REFERENCE IMPLEMENTATION OF A CUSTOM GUARDRAIL.

This guard demonstrates everything an author needs to do to add a new
guardrail to BankBuddy. The companion authoring guide lives at
`docs/guardrails.md` and walks through the same checklist.

WHAT IT DOES
------------
Refuse user inputs that are clearly off-topic for a banking assistant
(e.g. "write me a poem about cats"). This protects the LLM budget from
abuse and keeps the assistant on-mission.

HOW IT DECIDES
--------------
Two-stage scoring:
    1. Allow-list: if the input contains any banking keyword
       (configurable), the guard ALLOWS immediately - cheap and
       deterministic.
    2. Otherwise: compute a *banking-ratio* = matched_banking_terms /
       total_meaningful_words. If that ratio is below `min_ratio` AND
       the input is longer than `min_length` characters, BLOCK with a
       polite refusal reason.

CONFIGURATION KEYS (set via `GUARD_BANKING_RELEVANCE_CONFIG`, JSON object)
------------------------------------------------------------------------
    keywords        list[str]   banking vocabulary; default below
    min_ratio       float       0..1, default 0.05 - tune per locale
    min_length      int         skip very short inputs ("hi", "ok")
    refusal_message str         shown to the user when blocked

EXAMPLE
-------
    GUARD_BANKING_RELEVANCE_ENABLED=true
    GUARD_BANKING_RELEVANCE_CONFIG='{
        "min_ratio": 0.08,
        "keywords": ["account","balance","loan","mortgage","atm","card"],
        "refusal_message": "I can only help with banking questions."
    }'

EXTENDING
---------
For higher recall, swap the keyword check for an LLM-based judge by
overriding `_score()`. The pipeline contract (return `GuardCheckResult`)
stays the same - that's why this is a single-file change.

CHECKLIST FOR CUSTOM GUARDS (also in docs/guardrails.md)
--------------------------------------------------------
    [ ] Subclass `Guard`.
    [ ] Set `name`, `stage`, `description` class attributes.
    [ ] Accept config via `__init__(**config)` and store on `self.config`.
    [ ] Implement async `check(text, *, context)` returning a
        `GuardCheckResult`. Use `self._allow / _sanitize / _block`.
    [ ] Never raise; the pipeline treats raises as ALLOW with a warning.
    [ ] Register at module load: `register_guard("<name>", factory)`.
    [ ] Import the module in `guards/__init__.py`.
    [ ] Add an entry to `DEFAULT_INPUT_ORDER` or `DEFAULT_OUTPUT_ORDER`
        in `registry.py`.
    [ ] Document env keys in `.env.example` and `docs/guardrails.md`.
    [ ] Add a unit test in `tests/guardrails/`.
"""
from __future__ import annotations

import re
from typing import Any

from ..base import Guard, GuardCheckResult, GuardStage
from ..registry import register_guard

DEFAULT_KEYWORDS = [
    "account", "accounts", "balance", "balances",
    "transaction", "transactions", "transfer", "transfers", "wire",
    "card", "credit", "debit", "atm", "branch",
    "loan", "loans", "mortgage", "interest", "apr",
    "deposit", "withdraw", "withdrawal", "statement",
    "payment", "payments", "bill", "billing",
    "savings", "checking", "currency", "fx", "fees",
    "fraud", "block", "freeze",
    "bank", "banking"
]

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'-]+")


class BankingRelevanceGuard(Guard):
    name = "banking-relevance"
    stage = GuardStage.INPUT
    description = "Refuse off-topic inputs (anything not banking-related)."

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)
        self.keywords: set[str] = {w.lower() for w in config.get("keywords", DEFAULT_KEYWORDS)}
        self.min_ratio: float = float(config.get("min_ratio", 0.05))
        self.min_length: int = int(config.get("min_length", 3))
        self.refusal_message: str = str(
            config.get(
                "refusal_message",
                "I can only help with banking topics like accounts, transfers, cards, ATMs, or loans.",
            )
        )

    async def check(self, text: str, *, context: dict[str, Any] | None = None) -> GuardCheckResult:
        if len(text.strip()) < self.min_length:
            # Short greetings / acknowledgements - let through.
            return self._allow(text, metadata={"skipped": "too-short"})

        words = [w.lower() for w in _WORD_RE.findall(text)]
        if not words:
            return self._allow(text, metadata={"skipped": "no-words"})

        matched = [w for w in words if w in self.keywords]
        ratio = len(matched) / len(words)
        if matched:
            # Has at least one banking term - lenient allow.
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


register_guard("banking-relevance", lambda cfg: BankingRelevanceGuard(**cfg))
