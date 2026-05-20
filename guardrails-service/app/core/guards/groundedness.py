"""groundedness: detect hallucinated / unsupported claims in the model output.

WHY THIS GUARD EXISTS
---------------------
Azure AI Content Safety ships a `detectGroundedness` API but it is still
in preview. This guard fills that gap with a fully offline default
implementation while keeping the same `Guard` contract, so once the
managed API goes GA we can add an ``engine="azure"`` branch with zero
pipeline / caller changes.

HOW IT DECIDES
--------------
Given an LLM reply and a list of source passages (passed in via
``context["sources"]``), the guard scores how much of the reply is
*supported* by the sources.

Engines:
    * ``"overlap"`` (default, offline, deterministic)
        Tokenize the reply into sentences, then score each sentence by
        the fraction of meaningful tokens that also appear in any source
        passage. The reply's groundedness score is the mean per-sentence
        score (with optional length weighting).
    * ``"nli"`` (placeholder)
        Reserved for a cross-encoder NLI model (e.g. deberta-v3 MNLI)
        scoring entailment per sentence vs. sources. Falls back to the
        overlap engine until a model handle is wired in.
    * ``"llm-judge"`` (placeholder)
        Reserved for a small judge prompt; same fallback behavior.
    * ``"azure"`` (placeholder)
        Reserved for Azure AI Content Safety ``text:detectGroundedness``
        once GA. Falls back to overlap until wired.

DECISIONS
---------
    * If no sources are supplied AND ``require_sources=true``: BLOCK
      (the model produced an answer with no evidence to ground against).
    * If sources supplied and score < ``block_threshold``: BLOCK.
    * If sources supplied and score < ``warn_threshold``: SANITIZE
      (append ``unverified_suffix`` to the reply).
    * Otherwise: ALLOW.

CONFIGURATION KEYS (set via ``GUARD_GROUNDEDNESS_CONFIG``, JSON object)
----------------------------------------------------------------------
    engine             "overlap" | "nli" | "llm-judge" | "azure"
    block_threshold    float 0..1, default 0.45
    warn_threshold     float 0..1, default 0.65
    require_sources    bool, default false. When true, BLOCK if
                       ``context["sources"]`` is missing/empty.
    min_length         int, default 40. Skip very short replies
                       ("ok", "thanks") - they have nothing to ground.
    unverified_suffix  str, default "\\n\\n_(unverified - no sources
                       provided)_"
    block_message      str, refusal text appended to BLOCK reasons.

EXAMPLE
-------
    GUARD_GROUNDEDNESS_ENABLED=true
    GUARD_GROUNDEDNESS_CONFIG='{
        "engine": "overlap",
        "block_threshold": 0.4,
        "warn_threshold": 0.7,
        "require_sources": true
    }'

The agent must pass retrieved chunks into the pipeline as
``context={"sources": ["passage 1", "passage 2", ...]}`` when invoking
the output stage. The pipeline already forwards `context` to every
guard's `check()` call.
"""
from __future__ import annotations

import re
from typing import Any

from ..base import Guard, GuardCheckResult, GuardStage
from ..registry import register_guard

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'-]+")
_SENT_RE = re.compile(r"(?<=[.!?])\s+")

# Very small English stopword set - kept tiny on purpose so we do not
# accidentally drop banking nouns ("account", "balance", ...).
_STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "and", "or", "but", "if", "then", "of", "to",
    "in", "on", "at", "for", "with", "is", "are", "was", "were", "be",
    "been", "being", "as", "by", "it", "its", "this", "that", "these",
    "those", "i", "you", "your", "we", "our", "they", "them", "from",
    "so", "do", "does", "did", "not", "no", "yes",
})


def _tokens(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text) if w.lower() not in _STOPWORDS]


def _sentences(text: str) -> list[str]:
    parts = [s.strip() for s in _SENT_RE.split(text.strip()) if s.strip()]
    return parts or ([text.strip()] if text.strip() else [])


class GroundednessGuard(Guard):
    name = "groundedness"
    stage = GuardStage.OUTPUT
    description = (
        "Score model output against retrieved sources. BLOCK or flag "
        "replies that are not supported by the provided evidence."
    )

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)
        self.engine: str = str(config.get("engine", "overlap")).lower()
        self.block_threshold: float = float(config.get("block_threshold", 0.45))
        self.warn_threshold: float = float(config.get("warn_threshold", 0.65))
        self.require_sources: bool = bool(config.get("require_sources", False))
        self.min_length: int = int(config.get("min_length", 40))
        self.unverified_suffix: str = str(
            config.get("unverified_suffix", "\n\n_(unverified - no sources provided)_")
        )
        self.block_message: str = str(
            config.get(
                "block_message",
                "I can't verify that against the available sources, so I'm not going to answer.",
            )
        )

    async def check(self, text: str, *, context: dict[str, Any] | None = None) -> GuardCheckResult:
        reply = (text or "").strip()
        if len(reply) < self.min_length:
            return self._allow(text, metadata={"skipped": "too-short"})

        sources_raw = (context or {}).get("sources") or []
        sources: list[str] = [s for s in (str(x) for x in sources_raw) if s.strip()]

        if not sources:
            if self.require_sources:
                return self._block(
                    text,
                    reasons=[
                        "no sources provided to ground against",
                        self.block_message,
                    ],
                    categories=["rai.groundedness.no-sources"],
                    score=0.0,
                    metadata={"engine": self.engine, "sources_count": 0},
                )
            return self._sanitize(
                reply + self.unverified_suffix,
                reasons=["no sources to verify against; flagged as unverified"],
                categories=["rai.groundedness.unverified"],
                score=0.0,
                metadata={"engine": self.engine, "sources_count": 0},
            )

        score, per_sentence = self._score(reply, sources)

        if score < self.block_threshold:
            unsupported = [s for s, sc in per_sentence if sc < self.block_threshold]
            return self._block(
                text,
                reasons=[
                    f"groundedness score {score:.2f} < block_threshold {self.block_threshold:.2f}",
                    self.block_message,
                ],
                categories=["rai.groundedness.unsupported"],
                score=score,
                metadata={
                    "engine": self.engine,
                    "sources_count": len(sources),
                    "unsupported_sentences": unsupported[:5],
                },
            )

        if score < self.warn_threshold:
            return self._sanitize(
                reply + self.unverified_suffix,
                reasons=[
                    f"groundedness score {score:.2f} < warn_threshold {self.warn_threshold:.2f}",
                ],
                categories=["rai.groundedness.weak"],
                score=score,
                metadata={"engine": self.engine, "sources_count": len(sources)},
            )

        return self._allow(
            text,
            score=score,
            metadata={"engine": self.engine, "sources_count": len(sources)},
        )

    # ------------------------------------------------------------------
    # Engines
    # ------------------------------------------------------------------

    def _score(self, reply: str, sources: list[str]) -> tuple[float, list[tuple[str, float]]]:
        """Return (overall_score, [(sentence, sentence_score), ...]).

        Only the ``overlap`` engine is wired here. The other engines are
        placeholders that intentionally fall back to overlap so the
        guard stays useful before ML deps / managed APIs are added.
        """
        # Future: branch on self.engine to call cross-encoder / LLM judge / Azure.
        return self._score_overlap(reply, sources)

    @staticmethod
    def _score_overlap(reply: str, sources: list[str]) -> tuple[float, list[tuple[str, float]]]:
        source_tokens: set[str] = set()
        for s in sources:
            source_tokens.update(_tokens(s))

        if not source_tokens:
            return 0.0, []

        per_sentence: list[tuple[str, float]] = []
        weighted_sum = 0.0
        weight_total = 0
        for sent in _sentences(reply):
            toks = _tokens(sent)
            if not toks:
                continue
            matched = sum(1 for t in toks if t in source_tokens)
            sc = matched / len(toks)
            per_sentence.append((sent, sc))
            weighted_sum += sc * len(toks)
            weight_total += len(toks)

        overall = (weighted_sum / weight_total) if weight_total else 0.0
        return overall, per_sentence


register_guard("groundedness", lambda cfg: GroundednessGuard(**cfg))
