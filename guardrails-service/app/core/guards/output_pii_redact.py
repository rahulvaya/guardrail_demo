"""output-pii-redact: redact obvious PII from the assistant's reply.

Similar to `pii-detect` but always SANITIZE (never BLOCK). This is the
*output* counterpart so the model can never accidentally echo a full
SSN, card number, or IBAN even if it appears in upstream data.

CONFIGURATION KEYS (policy YAML or ``GUARD_OUTPUT_PII_REDACT_CONFIG`` JSON)
--------------------------------------------------------------------------
    patterns        dict[str, str | dict] - label -> regex string OR
                    {regex, mask}. When provided, REPLACES the built-in
                    ``DEFAULT_PATTERNS`` entirely.
    extra_patterns  dict[str, str | dict] - MERGED on top of the active
                    pattern set. Use to extend without redefining.

Mask may be a fixed string. The built-in credit-card and IBAN masks
preserve the last 4 / first 4 characters; supply your own static mask
string in config if you prefer full opacity.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable

from ..base import Guard, GuardCheckResult, GuardStage
from ..registry import register_guard

log = logging.getLogger("agent.guardrails.output_pii_redact")


def _mask_card(s: str) -> str:
    digits = re.sub(r"\D", "", s)
    if len(digits) < 4:
        return "****"
    return "**** **** **** " + digits[-4:]


# Built-in defaults. Each entry: label -> {regex, mask}. ``mask`` is
# either a plain string OR one of the named callables in ``_MASK_FUNCS``.
DEFAULT_PATTERNS: dict[str, dict[str, str]] = {
    "ssn":         {"regex": r"\b\d{3}-\d{2}-\d{4}\b", "mask": "***-**-****"},
    "credit-card": {"regex": r"\b(?:\d[ -]?){13,19}\b", "mask": "@card"},
    "iban":        {"regex": r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b", "mask": "@iban"},
}

_MASK_FUNCS: dict[str, Callable[[re.Match[str]], str]] = {
    "@card": lambda m: _mask_card(m.group()),
    "@iban": lambda m: m.group()[:4] + "*" * 6,
}


def _compile_patterns(spec: dict[str, Any]) -> list[tuple[str, re.Pattern[str], Any]]:
    out: list[tuple[str, re.Pattern[str], Any]] = []
    for label, entry in (spec or {}).items():
        try:
            if isinstance(entry, str):
                expr, mask_spec = entry, f"<{str(label).upper()}>"
            elif isinstance(entry, dict):
                expr = entry.get("regex") or entry.get("pattern")
                mask_spec = entry.get("mask", f"<{str(label).upper()}>")
            else:
                continue
            if not expr:
                continue
            repl: Any = _MASK_FUNCS.get(str(mask_spec), mask_spec)
            out.append((str(label), re.compile(expr), repl))
        except re.error as exc:
            log.warning("output-pii-redact: bad regex %r for %r: %s", expr, label, exc)
    return out


class OutputPiiRedactGuard(Guard):
    name = "output-pii-redact"
    stage = GuardStage.OUTPUT
    description = "Mask SSN / credit card / IBAN in the assistant's response."

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)
        raw: dict[str, Any] = dict(config.get("patterns") or DEFAULT_PATTERNS)
        extra = config.get("extra_patterns") or {}
        if isinstance(extra, dict):
            raw.update(extra)
        self._patterns: list[tuple[str, re.Pattern[str], Any]] = _compile_patterns(raw)

    async def check(self, text: str, *, context: dict[str, Any] | None = None) -> GuardCheckResult:
        sanitized = text
        hits: list[str] = []
        for label, pat, repl in self._patterns:
            new, n = pat.subn(repl, sanitized)
            if n > 0:
                hits.append(f"{label} x{n}")
                sanitized = new
        if not hits:
            return self._allow(text)
        return self._sanitize(sanitized, reasons=hits, categories=[f"pii.{h.split()[0]}" for h in hits])


register_guard("output-pii-redact", lambda cfg: OutputPiiRedactGuard(**cfg))
