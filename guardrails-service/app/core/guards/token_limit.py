"""token-limit: refuse pathologically long inputs (cheap DoS guard)."""
from __future__ import annotations

from typing import Any

from ..base import Guard, GuardCheckResult, GuardStage
from ..registry import register_guard


class TokenLimitGuard(Guard):
    name = "token-limit"
    stage = GuardStage.INPUT
    description = "Reject inputs longer than `max_chars` (cheap proxy for token count)."

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)
        self.max_chars: int = int(config.get("max_chars", 8000))

    async def check(self, text: str, *, context: dict[str, Any] | None = None) -> GuardCheckResult:
        if len(text) > self.max_chars:
            return self._block(
                text,
                reasons=[f"input length {len(text)} > max_chars {self.max_chars}"],
                categories=["abuse.length"],
                metadata={"length": len(text)},
            )
        return self._allow(text, metadata={"length": len(text)})


register_guard("token-limit", lambda cfg: TokenLimitGuard(**cfg))
