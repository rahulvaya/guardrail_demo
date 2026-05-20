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

# Each pattern is (regex_string, severity_weight). Score = sum of weights.
# Configurable: override the full list with ``patterns`` or merge extras
# via ``extra_patterns`` (both: list of {regex, weight} or {pattern, weight}).
DEFAULT_PATTERNS: list[tuple[str, float]] = [
    (r"\bignore (all|any|previous|prior|the above) (instructions?|prompts?|rules?)\b", 0.9),
    (r"\bdisregard (all|any|previous|prior) (instructions?|rules?)\b", 0.9),
    (r"\b(you are|act as|pretend to be) (?:a |an )?(?:DAN|jailbroken|unrestricted)\b", 0.95),
    (r"\bdeveloper mode\b", 0.7),
    (r"\bsystem prompt\b", 0.5),
    (r"\boverride your (instructions|guardrails|safety)\b", 0.9),
    (r"</?(system|assistant|user)>", 0.8),    # role-tag injection
    (r"\\x[0-9a-f]{2}", 0.3),                  # hex escape spam
    (r"base64:\s*[A-Za-z0-9+/=]{40,}", 0.4),
    (r"\brepeat (?:back|verbatim) your (?:system|initial) prompt\b", 0.95),
]

log = __import__("logging").getLogger("agent.guardrails.prompt_injection")


def _coerce_patterns(items: Any) -> list[tuple[str, float]]:
    """Accepts list of [pattern,weight] pairs OR {pattern|regex, weight} dicts."""
    out: list[tuple[str, float]] = []
    if not items:
        return out
    for it in items:
        try:
            if isinstance(it, dict):
                expr = it.get("pattern") or it.get("regex")
                weight = float(it.get("weight", 0.5))
            elif isinstance(it, (list, tuple)) and len(it) == 2:
                expr, weight = it[0], float(it[1])
            else:
                continue
            if not expr:
                continue
            out.append((str(expr), float(weight)))
        except (TypeError, ValueError):
            continue
    return out


class PromptInjectionGuard(Guard):
    name = "prompt-injection"
    stage = GuardStage.INPUT
    description = "Heuristic prompt-injection / jailbreak detector."

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)
        # Score >= block_threshold -> BLOCK. Default 0.7.
        self.block_threshold: float = float(config.get("block_threshold", 0.7))
        override = _coerce_patterns(config.get("patterns")) if config.get("patterns") else None
        base: list[tuple[str, float]] = override if override else list(DEFAULT_PATTERNS)
        base.extend(_coerce_patterns(config.get("extra_patterns")))
        compiled: list[tuple[re.Pattern[str], float]] = []
        for expr, weight in base:
            try:
                compiled.append((re.compile(expr, re.I), weight))
            except re.error as exc:
                log.warning("prompt-injection: bad regex %r: %s", expr, exc)
        self._patterns: list[tuple[re.Pattern[str], float]] = compiled

    async def check(self, text: str, *, context: dict[str, Any] | None = None) -> GuardCheckResult:
        score = 0.0
        matched: list[str] = []
        for pat, weight in self._patterns:
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
