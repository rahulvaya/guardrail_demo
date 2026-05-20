"""prompt-injection: detect jailbreak / instruction-override attempts.

Heuristic baseline (no GPU required). Flags well-known patterns from the
prompt-injection literature: role-override, system-prompt leakage, tool
override, encoding tricks. Production deployments should layer
Prompt-Guard-86M or LLM Guard `PromptInjection` on top - see
`docs/guardrails.md` for how to swap implementations.
"""
from __future__ import annotations

import re
from typing import Any

from ..base import Guard, GuardCheckResult, GuardStage
from ..registry import register_guard

# Each pattern is (regex, severity_weight). Score = sum of weights.
PATTERNS: list[tuple[re.Pattern[str], float]] = [
    (re.compile(r"\bignore (all|any|previous|prior|the above) (instructions?|prompts?|rules?)\b", re.I), 0.9),
    (re.compile(r"\bdisregard (all|any|previous|prior) (instructions?|rules?)\b", re.I), 0.9),
    (re.compile(r"\b(you are|act as|pretend to be) (?:a |an )?(?:DAN|jailbroken|unrestricted)\b", re.I), 0.95),
    (re.compile(r"\bdeveloper mode\b", re.I), 0.7),
    (re.compile(r"\bsystem prompt\b", re.I), 0.5),
    (re.compile(r"\boverride your (instructions|guardrails|safety)\b", re.I), 0.9),
    (re.compile(r"</?(system|assistant|user)>", re.I), 0.8),    # role-tag injection
    (re.compile(r"\\x[0-9a-f]{2}", re.I), 0.3),                  # hex escape spam
    (re.compile(r"base64:\s*[A-Za-z0-9+/=]{40,}", re.I), 0.4),
    (re.compile(r"\brepeat (?:back|verbatim) your (?:system|initial) prompt\b", re.I), 0.95),
]


class PromptInjectionGuard(Guard):
    name = "prompt-injection"
    stage = GuardStage.INPUT
    description = "Heuristic prompt-injection / jailbreak detector."

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)
        # Score >= block_threshold -> BLOCK. Default 0.7.
        self.block_threshold: float = float(config.get("block_threshold", 0.7))

    async def check(self, text: str, *, context: dict[str, Any] | None = None) -> GuardCheckResult:
        score = 0.0
        matched: list[str] = []
        for pat, weight in PATTERNS:
            if pat.search(text):
                score += weight
                matched.append(pat.pattern)

        if score >= self.block_threshold:
            return self._block(
                text,
                reasons=[f"prompt-injection score {score:.2f} >= {self.block_threshold}"],
                categories=["security.jailbreak"],
                score=min(score, 1.0),
                metadata={"matches": matched},
            )
        if matched:
            # Suspicious but below threshold - allow but record.
            return self._allow(text, score=score, metadata={"matches": matched})
        return self._allow(text, score=0.0)


register_guard("prompt-injection", lambda cfg: PromptInjectionGuard(**cfg))
