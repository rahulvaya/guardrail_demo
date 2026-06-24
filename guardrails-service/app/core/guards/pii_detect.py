"""pii-detect: flag PII in INPUT or OUTPUT.

Default engine: **Microsoft Presidio** (https://github.com/microsoft/presidio).
Falls back to a regex engine when ``presidio-analyzer`` is not installed
or fails to initialize, so the guard never disappears from the pipeline.

By default this guard SANITIZES (masks the entity) rather than blocking,
so users can still ask "what's my balance?" even if their question
mentions an email address. Switch to BLOCK by setting
``mode: "block"`` in policy YAML or ``GUARD_PII_DETECT_CONFIG``.

Configuration keys (policy YAML or ``GUARD_PII_DETECT_CONFIG`` JSON):
    engine          "presidio" (default) | "regex"
    mode            "sanitize" (default) | "block" | "redact_and_block"
    language        Presidio language code (default: "en")
    min_score       Float in [0, 1]; entities below this are ignored
                    when engine == "presidio" (default: 0.4)
    entities        Optional list[str] to whitelist Presidio entity types
                    (e.g. ["EMAIL_ADDRESS", "CREDIT_CARD"]). Empty / unset
                    => analyze all built-in entities.
    spacy_model     spaCy model name for the Presidio NLP engine
                    (default: "en_core_web_sm"). Falls back to whichever
                    model is actually installed on the host.
    patterns        dict[label -> regex | {regex, mask}] for regex engine.
                    Overrides ``DEFAULT_PATTERNS`` when present.
    extra_patterns  dict merged on top of the active regex pattern set.
"""
from __future__ import annotations

import re
from typing import Any

from ..base import Guard, GuardCheckResult, GuardDecision, GuardStage
from ..observability import obs_log
from ..registry import register_guard

# ---------------------------------------------------------------------------
# Regex fallback / supplement patterns
# ---------------------------------------------------------------------------

DEFAULT_PATTERNS: dict[str, dict[str, str]] = {
    "email":       {"regex": r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b",                    "mask": "<EMAIL>"},
    "ssn":         {"regex": r"\b\d{3}-\d{2}-\d{4}\b",                           "mask": "<SSN>"},
    "credit-card": {"regex": r"\b(?:\d[ -]?){13,19}\b",                          "mask": "<CARD>"},
    # US phone: handles +1-xxx, (xxx) xxx-xxxx, and bare 10-digit forms.
    "us-phone":    {"regex": r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b", "mask": "<PHONE>"},
    # International phone: +CC followed by 6-14 digits/separators.
    "intl-phone":  {"regex": r"\+\d{1,3}[\s\-.][ \d\s\-.]{6,}\d",               "mask": "<PHONE>"},
    "ipv4":        {"regex": r"\b(?:\d{1,3}\.){3}\d{1,3}\b",                     "mask": "<IP>"},
    "iban":        {"regex": r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b",                "mask": "<IBAN>"},
    "date-iso":    {"regex": r"\b\d{4}-\d{2}-\d{2}\b",                           "mask": "<DATE>"},
}


def _compile_patterns(
    spec: dict[str, Any],
    guard_name: str = "pii-detect",
) -> list[tuple[str, re.Pattern[str], str]]:
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
            obs_log(
                "guard.regex_invalid",
                level="warning",
                guard=guard_name,
                label=str(label),
                error=str(exc),
            )
    return out


def _build_presidio_engine(
    language: str = "en",
    spacy_model: str = "en_core_web_sm",
) -> Any:
    from presidio_analyzer import AnalyzerEngine  # type: ignore
    from presidio_analyzer.nlp_engine import NlpEngineProvider  # type: ignore

    provider = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": language, "model_name": spacy_model}],
        }
    )
    nlp_engine = provider.create_engine()
    return AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=[language])


def _analyze_and_redact(
    text: str,
    *,
    presidio_engine: Any | None,
    patterns: list[tuple[str, re.Pattern[str], str]],
    language: str = "en",
    min_score: float = 0.4,
    entities: list[str] | None = None,
) -> tuple[str, list[str], list[str]]:
    presidio_results: list[Any] = []
    if presidio_engine is not None:
        kwargs: dict[str, Any] = {"text": text, "language": language}
        if entities:
            kwargs["entities"] = entities
        all_results = presidio_engine.analyze(**kwargs)
        presidio_results = [r for r in all_results if getattr(r, "score", 0.0) >= min_score]

    # Apply Presidio replacements (reverse order to preserve offsets).
    sanitized = text
    for r in sorted(presidio_results, key=lambda r: r.start, reverse=True):
        sanitized = sanitized[: r.start] + f"<{r.entity_type}>" + sanitized[r.end :]

    # Apply regex supplement on top.
    regex_found: list[str] = []
    for label, pat, mask in (patterns or []):
        new, n = pat.subn(mask, sanitized)
        if n > 0:
            regex_found.append(f"{label} x{n}")
            sanitized = new

    if not presidio_results and not regex_found:
        return text, [], []

    reasons = (
        [f"{r.entity_type} (score={r.score:.2f})" for r in presidio_results]
        + regex_found
    )
    categories = (
        [f"pii.{r.entity_type.lower()}" for r in presidio_results]
        + [f"pii.{f.split()[0]}" for f in regex_found]
    )
    return sanitized, reasons, categories


class PiiDetectGuard(Guard):
    name = "pii-detect"
    stage = GuardStage.INPUT
    description = "Detect PII via Microsoft Presidio (regex fallback) and mask or block."

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)
        self.mode: str = str(config.get("mode", "sanitize")).lower()  # sanitize | block | redact_and_block
        self.engine: str = str(config.get("engine", "presidio")).lower()  # presidio | regex
        self.language: str = str(config.get("language", "en"))
        self.min_score: float = float(config.get("min_score", 0.4))
        entities = config.get("entities") or []
        self.entities: list[str] = [str(e) for e in entities] if isinstance(entities, list) else []
        self.spacy_model: str = str(config.get("spacy_model", "en_core_web_sm"))

        raw: dict[str, Any] = dict(config.get("patterns") or DEFAULT_PATTERNS)
        extra = config.get("extra_patterns") or {}
        if isinstance(extra, dict):
            raw.update(extra)
        self._patterns = _compile_patterns(raw)

        self._presidio: Any = None
        if self.engine == "presidio":
            try:
                self._presidio = _build_presidio_engine(self.language, self.spacy_model)
                obs_log(
                    "guard.engine_ready",
                    guard="pii-detect",
                    engine="presidio",
                    language=self.language,
                    spacy_model=self.spacy_model,
                    min_score=self.min_score,
                    entities=self.entities or "all",
                )
            except Exception as exc:  # noqa: BLE001
                obs_log(
                    "guard.engine_fallback",
                    level="warning",
                    guard="pii-detect",
                    requested_engine="presidio",
                    fallback_engine="regex",
                    reason=f"{type(exc).__name__}: {exc}",
                )
                self.engine = "regex"

    async def check(self, text: str, *, context: dict[str, Any] | None = None) -> GuardCheckResult:
        presidio = self._presidio if self.engine == "presidio" else None
        sanitized, reasons, cats = _analyze_and_redact(
            text,
            presidio_engine=presidio,
            patterns=self._patterns,
            language=self.language,
            min_score=self.min_score,
            entities=self.entities or None,
        )
        if not reasons:
            return self._allow(text)
        if self.mode == "block":
            return self._block(text, reasons=reasons, categories=cats)
        if self.mode == "redact_and_block":
            return GuardCheckResult(
                guard_name=self.name,
                decision=GuardDecision.BLOCK,
                sanitized_text=sanitized,
                reasons=reasons,
                categories=cats,
            )
        return self._sanitize(sanitized, reasons=reasons, categories=cats)


register_guard("pii-detect", lambda cfg: PiiDetectGuard(**cfg))
