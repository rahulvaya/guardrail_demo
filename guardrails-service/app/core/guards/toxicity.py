"""toxicity: heuristic profanity / slur detection on outputs.

Production should plug in `detoxify` (pure-CPU PyTorch) by setting
`engine="detoxify"`. The default keyword list is intentionally minimal -
the goal is to demonstrate the integration point, not ship a profanity
corpus.
"""
from __future__ import annotations

import logging
from typing import Any

from ..base import Guard, GuardCheckResult, GuardStage
from ..registry import register_guard

log = logging.getLogger("agent.guardrails.toxicity")

# Minimal placeholder list - replace via GUARD_TOXICITY_CONFIG='{"words":[...]}'.
DEFAULT_WORDS = ["idiot", "stupid", "moron", "shut up"]


class ToxicityGuard(Guard):
    name = "toxicity"
    stage = GuardStage.OUTPUT
    description = "Detect toxic / abusive language in the assistant's reply."

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)
        self.engine: str = str(config.get("engine", "keyword")).lower()
        self.threshold: float = float(config.get("threshold", 0.7))
        self.words: list[str] = [w.lower() for w in config.get("words", DEFAULT_WORDS)]
        self._detox = None
        if self.engine == "detoxify":
            try:  # pragma: no cover - optional dep
                from detoxify import Detoxify  # type: ignore
                self._detox = Detoxify("original-small")
            except Exception:  # noqa: BLE001
                log.warning("detoxify not available; falling back to keyword engine")
                self.engine = "keyword"

    async def check(self, text: str, *, context: dict[str, Any] | None = None) -> GuardCheckResult:
        if self.engine == "detoxify" and self._detox is not None:
            return self._check_detox(text)  # pragma: no cover
        return self._check_keyword(text)

    def _check_keyword(self, text: str) -> GuardCheckResult:
        lowered = text.lower()
        hits = [w for w in self.words if w in lowered]
        if not hits:
            return self._allow(text, score=0.0)
        return self._block(
            text,
            reasons=[f"toxic word: {w}" for w in hits],
            categories=["harm.toxicity"],
            score=1.0,
            metadata={"matches": hits, "engine": "keyword"},
        )

    def _check_detox(self, text: str) -> GuardCheckResult:  # pragma: no cover
        scores = self._detox.predict(text)  # type: ignore[union-attr]
        toxic_score = float(scores.get("toxicity", 0.0))
        if toxic_score >= self.threshold:
            return self._block(
                text,
                reasons=[f"detoxify toxicity {toxic_score:.2f} >= {self.threshold}"],
                categories=["harm.toxicity"],
                score=toxic_score,
                metadata={"scores": scores},
            )
        return self._allow(text, score=toxic_score, metadata={"scores": scores})


register_guard("toxicity", lambda cfg: ToxicityGuard(**cfg))
