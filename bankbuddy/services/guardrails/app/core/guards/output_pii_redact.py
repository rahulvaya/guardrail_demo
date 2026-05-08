"""output-pii-redact: redact obvious PII from the assistant's reply.

Same regex set as `pii-detect` but always SANITIZE (never BLOCK).
This is the *output* counterpart so the model can never accidentally
echo a full SSN, card number, or IBAN even if it appears in mock-bank
data.
"""
from __future__ import annotations

import re
from typing import Any

from ..base import Guard, GuardCheckResult, GuardStage
from ..registry import register_guard

OUTPUT_PATTERNS: list[tuple[str, re.Pattern[str], Any]] = [
    ("ssn",         re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),                                    "***-**-****"),
    ("credit-card", re.compile(r"\b(?:\d[ -]?){13,19}\b"),                                   lambda m: _mask_card(m.group())),
    ("iban",        re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b"),                         lambda m: m.group()[:4] + "*" * 6),
]


def _mask_card(s: str) -> str:
    digits = re.sub(r"\D", "", s)
    if len(digits) < 4:
        return "****"
    return "**** **** **** " + digits[-4:]


class OutputPiiRedactGuard(Guard):
    name = "output-pii-redact"
    stage = GuardStage.OUTPUT
    description = "Mask SSN / credit card / IBAN in the assistant's response."

    async def check(self, text: str, *, context: dict[str, Any] | None = None) -> GuardCheckResult:
        sanitized = text
        hits: list[str] = []
        for label, pat, repl in OUTPUT_PATTERNS:
            if callable(repl):
                new, n = pat.subn(repl, sanitized)
            else:
                new, n = pat.subn(repl, sanitized)
            if n > 0:
                hits.append(f"{label} x{n}")
                sanitized = new
        if not hits:
            return self._allow(text)
        return self._sanitize(sanitized, reasons=hits, categories=[f"pii.{h.split()[0]}" for h in hits])


register_guard("output-pii-redact", lambda cfg: OutputPiiRedactGuard(**cfg))
