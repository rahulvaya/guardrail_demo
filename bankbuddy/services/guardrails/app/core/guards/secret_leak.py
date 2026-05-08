"""secret-leak: block outputs that contain credentials or API keys."""
from __future__ import annotations

import re
from typing import Any

from ..base import Guard, GuardCheckResult, GuardStage
from ..registry import register_guard

# (label, regex)
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws-access-key",  re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("aws-secret",      re.compile(r"(?i)\baws(.{0,20})?(secret|key)[\"'\s:=]+[A-Za-z0-9/+=]{40}\b")),
    ("github-pat",      re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("openai-key",      re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("private-key",     re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("jwt",             re.compile(r"\beyJ[A-Za-z0-9_=-]{10,}\.[A-Za-z0-9_=-]{10,}\.[A-Za-z0-9_=.+/-]{10,}\b")),
    ("bearer",          re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{20,}\b")),
]


class SecretLeakGuard(Guard):
    name = "secret-leak"
    stage = GuardStage.OUTPUT
    description = "Block outputs containing credentials, tokens, or private keys."

    async def check(self, text: str, *, context: dict[str, Any] | None = None) -> GuardCheckResult:
        hits: list[str] = []
        for label, pat in SECRET_PATTERNS:
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
