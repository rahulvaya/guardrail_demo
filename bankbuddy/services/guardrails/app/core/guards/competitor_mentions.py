"""competitor-mentions: example of a SANITIZE-style brand-safety guard.

Off by default (regulators differ; configure for your environment).
"""
from __future__ import annotations

import re
from typing import Any

from ..base import Guard, GuardCheckResult, GuardStage
from ..registry import register_guard

DEFAULT_COMPETITORS = ["AcmeBank", "GlobalBank", "MegaCorp Finance"]


class CompetitorMentionsGuard(Guard):
    name = "competitor-mentions"
    stage = GuardStage.OUTPUT
    description = "Replace competitor names in outputs with '<a competitor>'."

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)
        names = config.get("competitors") or DEFAULT_COMPETITORS
        self._patterns = [(n, re.compile(rf"\b{re.escape(n)}\b", re.IGNORECASE)) for n in names]

    async def check(self, text: str, *, context: dict[str, Any] | None = None) -> GuardCheckResult:
        sanitized = text
        hits: list[str] = []
        for name, pat in self._patterns:
            new, n = pat.subn("<a competitor>", sanitized)
            if n > 0:
                hits.append(name)
                sanitized = new
        if not hits:
            return self._allow(text)
        return self._sanitize(sanitized, reasons=[f"competitor: {n}" for n in hits], categories=["brand"])


register_guard("competitor-mentions", lambda cfg: CompetitorMentionsGuard(**cfg))
