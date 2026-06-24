"""secret-leak: block outputs that contain credentials or API keys.

CONFIGURATION KEYS (policy YAML or ``GUARD_SECRET_LEAK_CONFIG`` JSON)
--------------------------------------------------------------------
    patterns        dict[str, str] - label -> regex string. When
                    provided, REPLACES the built-in ``DEFAULT_PATTERNS``
                    entirely. Use to retarget the guard at your own
                    secret formats (vendor tokens, internal credentials).
    extra_patterns  dict[str, str] - label -> regex string. MERGED on
                    top of the active pattern set (defaults or
                    ``patterns``). Use to extend without redefining.

EXAMPLE
-------
    GUARD_SECRET_LEAK_CONFIG='{
        "extra_patterns": {
            "acme-token": "\\bacme_[A-Za-z0-9]{32}\\b"
        }
    }'
"""
from __future__ import annotations

import re
from typing import Any

from ..base import Guard, GuardCheckResult, GuardStage
from ..observability import obs_log
from ..registry import register_guard

# (label, regex). Built-in defaults; override or extend via config.
DEFAULT_PATTERNS: dict[str, str] = {
    # --- Cloud provider credentials ---
    "aws-access-key":  r"\bAKIA[0-9A-Z]{16}\b",
    "aws-secret":      r"(?i)\baws(.{0,20})?(secret|key)[\"'\s:=]+[A-Za-z0-9/+=]{40}\b",
    "github-pat":      r"\bghp_[A-Za-z0-9]{36}\b",
    "openai-key":      r"\bsk-[A-Za-z0-9]{20,}\b",
    "private-key":     r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    # --- Auth tokens ---
    "jwt":             r"\beyJ[A-Za-z0-9_=-]{10,}\.[A-Za-z0-9_=-]{10,}\.[A-Za-z0-9_=.+/-]{10,}\b",
    "bearer":          r"(?i)\bbearer\s+[A-Za-z0-9._-]{20,}\b",
    # --- Payment / SaaS vendor keys ---
    "stripe-key":      r"(?i)\bsk_(?:live|test)_[A-Za-z0-9]{20,}\b",
    "slack-token":     r"\bxox[bpoas]-[A-Za-z0-9-]{10,}\b",
    "slack-webhook":   r"hooks\.slack\.com/services/T[A-Z0-9]{6,}/B[A-Z0-9]{6,}/[A-Za-z0-9]{20,}",
    "gitlab-pat":      r"\bglpat-[A-Za-z0-9_-]{20,}\b",
    # --- Connection strings ---
    "azure-storage":   r"AccountKey=[A-Za-z0-9+/]{40,}={0,2}",
    "db-conn-url":     r"(?i)(?:postgresql|mysql|mongodb(?:\+srv)?|redis|mssql)://\S+@",
    # --- Generic credential patterns ---
    "password-value":  r"(?i)\bpassword\s*(?:is\s*)?[:=]\s*\S{8,}",
    "secret-keyword":  r"(?i)(?:secret|passwd|credential)\s*[:=]\s*\S{8,}",
    "generic-api-key": r"(?i)\bapi[_-]?key\s*[:=]\s*[A-Za-z0-9_-]{20,}",
    "token-keyword":   r"(?i)\btoken\s*[:=]\s*[A-Za-z0-9._-]{20,}",
}


class SecretLeakGuard(Guard):
    name = "secret-leak"
    stage = GuardStage.OUTPUT
    description = "Block outputs containing credentials, tokens, or private keys."

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)
        raw: dict[str, str] = dict(config.get("patterns") or DEFAULT_PATTERNS)
        extra = config.get("extra_patterns") or {}
        if isinstance(extra, dict):
            raw.update({str(k): str(v) for k, v in extra.items()})
        compiled: list[tuple[str, re.Pattern[str]]] = []
        for label, expr in raw.items():
            try:
                compiled.append((str(label), re.compile(expr)))
            except re.error as exc:
                obs_log(
                    "guard.regex_invalid",
                    level="warning",
                    guard="secret-leak",
                    label=str(label),
                    error=str(exc),
                )
        self._patterns: list[tuple[str, re.Pattern[str]]] = compiled

    async def check(self, text: str, *, context: dict[str, Any] | None = None) -> GuardCheckResult:
        hits: list[str] = []
        for label, pat in self._patterns:
            if pat.search(text):
                hits.append(label)
        if not hits:
            return self._allow(text)
        return self._block(
            text,
            reasons=[f"secret leak: {h}" for h in hits],
            categories=[f"security.secret.{h}" for h in hits],
            metadata={"matches": hits},
        )


register_guard("secret-leak", lambda cfg: SecretLeakGuard(**cfg))
