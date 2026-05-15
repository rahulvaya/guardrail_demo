"""bias-detect: flag stereotype / demographic-skew patterns in LLM output.

WHY THIS GUARD EXISTS
---------------------
Azure AI Content Safety covers ``Hate / Unfairness`` as a harm category
(via ``azure-content-safety``), but it does NOT explicitly catch softer
statistical biases - gender / role stereotypes, demographic skew in
recommendations, sycophancy, sentiment skew. This guard fills that gap
with a deterministic offline default and engine plug points for
Detoxify ``unbiased``, IBM HAP, and LLM-judge.

HOW IT DECIDES
--------------
Per-sentence pattern matching against a small stereotype lexicon
(category -> list of regexes). The reply's overall severity is the
max severity across matched categories. Severities:

    * ``"high"``   -> BLOCK
    * ``"medium"`` -> SANITIZE (rewrite matched phrases to neutral)
    * ``"low"``    -> ALLOW with metadata (telemetry only)

Engines:
    * ``"lexicon"`` (default, offline, deterministic)
    * ``"detoxify-unbiased"`` (placeholder, falls back to lexicon)
    * ``"hap"`` (placeholder, falls back to lexicon)
    * ``"llm-judge"`` (placeholder, falls back to lexicon)

CONFIGURATION KEYS (set via ``GUARD_BIAS_DETECT_CONFIG``, JSON object)
---------------------------------------------------------------------
    engine             "lexicon" | "detoxify-unbiased" | "hap" | "llm-judge"
    patterns           dict[str, list[str]] - category -> regex list
                       (overrides default lexicon entirely)
    severity_map       dict[str, str] - category -> "low"/"medium"/"high"
    neutral_replacement str, default "[neutralized]"
    block_message      str, refusal text on BLOCK
    min_length         int, default 30

EXAMPLE
-------
    GUARD_BIAS_DETECT_ENABLED=true
    GUARD_BIAS_DETECT_CONFIG='{
        "engine": "lexicon",
        "severity_map": {"gender-role": "high"}
    }'

LEXICON SOURCING
----------------
The defaults below are illustrative seeds, not a comprehensive bias
taxonomy. Replace via the ``patterns`` config in production using a
vetted source such as StereoSet or your organisation's RAI guidelines.
"""
from __future__ import annotations

import re
from typing import Any

from ..base import Guard, GuardCheckResult, GuardStage
from ..registry import register_guard

# Category -> list of regex strings (case-insensitive). Each pattern is a
# stereotype trigger phrase, NOT a slur. Keep this list small and
# defensible; treat it as a starting point.
DEFAULT_PATTERNS: dict[str, list[str]] = {
    "gender-role": [
        r"\bwomen\s+(?:are\s+)?(?:bad|worse|not\s+good)\s+at\b",
        r"\bmen\s+(?:are\s+)?(?:bad|worse|not\s+good)\s+at\b",
        r"\b(?:women|men)\s+(?:should|belong)\s+(?:in|at)\s+(?:home|kitchen)\b",
        r"\b(?:male|female)\s+nurses?\s+are\s+(?:rare|unusual|odd)\b",
        r"\bonly\s+(?:men|women)\s+can\b",
    ],
    "demographic-skew": [
        r"\bpeople\s+from\s+\w+\s+are\s+(?:typically|always|usually)\s+(?:bad|good|lazy|smart)\b",
        r"\ball\s+\w+\s+(?:people|customers)\s+are\b",
    ],
    "age-bias": [
        r"\b(?:old|elderly|young)\s+people\s+(?:cannot|can't|are\s+unable\s+to)\b",
        r"\btoo\s+(?:old|young)\s+to\s+(?:understand|handle|manage)\s+(?:money|finances|banking)\b",
    ],
    "ability-bias": [
        r"\b(?:disabled|handicapped)\s+(?:customers|people)\s+(?:cannot|can't)\b",
    ],
}

DEFAULT_SEVERITY: dict[str, str] = {
    "gender-role": "high",
    "demographic-skew": "high",
    "age-bias": "medium",
    "ability-bias": "high",
}

_SEV_RANK = {"low": 1, "medium": 2, "high": 3}


class BiasDetectGuard(Guard):
    name = "bias-detect"
    stage = GuardStage.OUTPUT
    description = (
        "Flag stereotype / demographic-skew patterns the Hate-Unfairness "
        "harm category does not explicitly cover."
    )

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)
        self.engine: str = str(config.get("engine", "lexicon")).lower()
        raw_patterns: dict[str, list[str]] = dict(
            config.get("patterns", DEFAULT_PATTERNS)
        )
        self.severity_map: dict[str, str] = {
            **DEFAULT_SEVERITY,
            **{str(k): str(v).lower() for k, v in config.get("severity_map", {}).items()},
        }
        self.neutral_replacement: str = str(config.get("neutral_replacement", "[neutralized]"))
        self.block_message: str = str(
            config.get(
                "block_message",
                "I won't generate content that reinforces stereotypes.",
            )
        )
        self.min_length: int = int(config.get("min_length", 30))

        self._compiled: dict[str, list[re.Pattern[str]]] = {}
        for cat, patterns in raw_patterns.items():
            compiled: list[re.Pattern[str]] = []
            for p in patterns:
                try:
                    compiled.append(re.compile(p, re.IGNORECASE))
                except re.error:
                    # Bad regex from config - skip, do not raise.
                    continue
            if compiled:
                self._compiled[cat] = compiled

    async def check(self, text: str, *, context: dict[str, Any] | None = None) -> GuardCheckResult:
        reply = text or ""
        if len(reply.strip()) < self.min_length:
            return self._allow(text, metadata={"skipped": "too-short"})

        matches: list[tuple[str, str, str]] = []  # (category, severity, snippet)
        max_severity = "low"
        for cat, patterns in self._compiled.items():
            sev = self.severity_map.get(cat, "low")
            for pat in patterns:
                for m in pat.finditer(reply):
                    matches.append((cat, sev, m.group(0)))
                    if _SEV_RANK.get(sev, 0) > _SEV_RANK.get(max_severity, 0):
                        max_severity = sev

        if not matches:
            return self._allow(text, score=0.0, metadata={"engine": self.engine})

        categories = sorted({f"rai.bias.{cat}" for cat, _, _ in matches})

        if max_severity == "high":
            return self._block(
                text,
                reasons=[
                    f"bias patterns matched (severity={max_severity})",
                    self.block_message,
                ],
                categories=categories,
                score=float(_SEV_RANK[max_severity]) / 3.0,
                metadata={
                    "engine": self.engine,
                    "matches": [
                        {"category": c, "severity": s, "snippet": snip}
                        for c, s, snip in matches[:10]
                    ],
                },
            )

        if max_severity == "medium":
            sanitized = reply
            for _, _, snippet in matches:
                sanitized = sanitized.replace(snippet, self.neutral_replacement)
            return self._sanitize(
                sanitized,
                reasons=[f"bias patterns rewritten (severity={max_severity})"],
                categories=categories,
                score=float(_SEV_RANK[max_severity]) / 3.0,
                metadata={
                    "engine": self.engine,
                    "matches": [
                        {"category": c, "severity": s, "snippet": snip}
                        for c, s, snip in matches[:10]
                    ],
                },
            )

        # low severity -> telemetry only
        return self._allow(
            text,
            score=float(_SEV_RANK[max_severity]) / 3.0,
            metadata={
                "engine": self.engine,
                "matches": [
                    {"category": c, "severity": s, "snippet": snip}
                    for c, s, snip in matches[:10]
                ],
            },
        )


register_guard("bias-detect", lambda cfg: BiasDetectGuard(**cfg))
