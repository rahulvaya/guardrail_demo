"""banned-substrings: hard-fail when text contains any configured phrase.

Equivalent to LLM Guard `BanSubstrings`. Use for known-bad regulated
phrases (account-takeover language, internal product code-names, etc.).
"""
from __future__ import annotations

from typing import Any

from ..base import Guard, GuardCheckResult, GuardStage
from ..registry import register_guard

DEFAULT_PHRASES = [
    # banking-fraud red flags
    "wire all funds",
    "transfer everything",
    "ignore previous instructions",
    "system prompt",
    "developer mode",
]


class BannedSubstringsGuard(Guard):
    name = "banned-substrings"
    stage = GuardStage.INPUT
    description = "Block inputs containing any configured banned phrase (case-insensitive)."

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)
        phrases = config.get("phrases") or DEFAULT_PHRASES
        self.phrases: list[str] = [p.lower() for p in phrases]

    async def check(self, text: str, *, context: dict[str, Any] | None = None) -> GuardCheckResult:
        lowered = text.lower()
        hits = [p for p in self.phrases if p in lowered]
        if hits:
            return self._block(
                text,
                reasons=[f"banned phrase: {p!r}" for p in hits],
                categories=["policy.blocklist"],
                metadata={"matches": hits},
            )
        return self._allow(text)


register_guard("banned-substrings", lambda cfg: BannedSubstringsGuard(**cfg))
