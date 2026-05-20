"""pii-detect: flag PII in INPUT.

Default: regex-based detection (no extra dependencies). Optional: if
`presidio-analyzer` is installed and `engine="presidio"` is set in the
guard config, defer to Presidio for higher recall.

By default this guard SANITIZES (masks the entity) rather than blocking,
so users can still ask "what's my balance?" even if their question
mentions an email address. Switch to BLOCK by setting
`mode: "block"` in `GUARD_PII_DETECT_CONFIG`.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from ..base import Guard, GuardCheckResult, GuardStage
from ..registry import register_guard

log = logging.getLogger("agent.guardrails.pii_detect")

# Conservative regex set. Each entry: label -> {regex, mask}.
# Configurable via policy YAML: override the whole set with ``patterns``
# or merge extras with ``extra_patterns``. Each value is either a regex
# string (mask defaults to ``<LABEL>``) or a {regex, mask} mapping.
DEFAULT_PATTERNS: dict[str, dict[str, str]] = {
    "email":       {"regex": r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b", "mask": "<EMAIL>"},
    "ssn":         {"regex": r"\b\d{3}-\d{2}-\d{4}\b", "mask": "<SSN>"},
    "credit-card": {"regex": r"\b(?:\d[ -]?){13,19}\b", "mask": "<CARD>"},
    # US phone: matches both formatted ((123) 456-7890, +1-234-567-8900) and
    # bare 10-digit (1234567890) variants. The bare form requires word
    # boundaries so it doesn't gobble unrelated long digit runs.
    "us-phone":    {"regex": r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b", "mask": "<PHONE>"},
    "ipv4":        {"regex": r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "mask": "<IP>"},
    "iban":        {"regex": r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b", "mask": "<IBAN>"},
}


def _compile_patterns(spec: dict[str, Any]) -> list[tuple[str, re.Pattern[str], str]]:
    out: list[tuple[str, re.Pattern[str], str]] = []
    for label, entry in (spec or {}).items():
        try:
            if isinstance(entry, str):
                expr, mask = entry, f"<{str(label).upper()}>"
            elif isinstance(entry, dict):
                expr = entry.get("regex") or entry.get("pattern")
                mask = str(entry.get("mask", f"<{str(label).upper()}>"))
            else:
                continue
            if not expr:
                continue
            out.append((str(label), re.compile(expr), mask))
        except re.error as exc:
            log.warning("pii-detect: bad regex %r for %r: %s", expr, label, exc)
    return out


class PiiDetectGuard(Guard):
    name = "pii-detect"
    stage = GuardStage.INPUT
    description = "Detect PII (email/SSN/card/phone/IP/IBAN) and either mask or block."

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)
        self.mode: str = str(config.get("mode", "sanitize")).lower()  # sanitize | block
        self.engine: str = str(config.get("engine", "regex")).lower()  # regex | presidio
        raw: dict[str, Any] = dict(config.get("patterns") or DEFAULT_PATTERNS)
        extra = config.get("extra_patterns") or {}
        if isinstance(extra, dict):
            raw.update(extra)
        self._patterns: list[tuple[str, re.Pattern[str], str]] = _compile_patterns(raw)
        self._presidio = None
        if self.engine == "presidio":
            try:
                from presidio_analyzer import AnalyzerEngine  # type: ignore
                self._presidio = AnalyzerEngine()
            except Exception:  # noqa: BLE001
                log.warning("presidio-analyzer not available; falling back to regex")
                self.engine = "regex"

    async def check(self, text: str, *, context: dict[str, Any] | None = None) -> GuardCheckResult:
        if self.engine == "presidio" and self._presidio is not None:
            return self._check_presidio(text)
        return self._check_regex(text)

    def _check_regex(self, text: str) -> GuardCheckResult:
        found: list[str] = []
        sanitized = text
        for label, pat, mask in self._patterns:
            new, n = pat.subn(mask, sanitized)
            if n > 0:
                found.append(f"{label} x{n}")
                sanitized = new
        if not found:
            return self._allow(text)
        if self.mode == "block":
            return self._block(text, reasons=found, categories=[f"pii.{f.split()[0]}" for f in found])
        return self._sanitize(sanitized, reasons=found, categories=[f"pii.{f.split()[0]}" for f in found])

    def _check_presidio(self, text: str) -> GuardCheckResult:  # pragma: no cover - optional path
        results = self._presidio.analyze(text=text, language="en")  # type: ignore[union-attr]
        if not results:
            return self._allow(text)
        sanitized = text
        # Sort descending by start so masking doesn't shift later offsets.
        for r in sorted(results, key=lambda r: r.start, reverse=True):
            sanitized = sanitized[: r.start] + f"<{r.entity_type}>" + sanitized[r.end :]
        reasons = [f"{r.entity_type} (score={r.score:.2f})" for r in results]
        cats = [f"pii.{r.entity_type.lower()}" for r in results]
        if self.mode == "block":
            return self._block(text, reasons=reasons, categories=cats)
        return self._sanitize(sanitized, reasons=reasons, categories=cats)


register_guard("pii-detect", lambda cfg: PiiDetectGuard(**cfg))
