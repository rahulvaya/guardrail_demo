"""banned-substrings: hard-fail when text contains any configured phrase.

Equivalent to LLM Guard `BanSubstrings`. Use for known-bad regulated
phrases (account-takeover language, internal product code-names, etc.).

Phrases can be supplied three ways (merged, case-insensitive, de-duped):

1. **Policy YAML** (`phrases:` list under the guard config) - baseline
   set shipped by ops.
2. **Per-request context** - any caller of ``/v1/check`` may pass
   ``context.banned_phrases`` (or ``context.extra_banned_phrases``) as
   a list[str] to extend the policy list for that single request. Use
   this for tenant-specific or session-specific blocklists.
3. **Built-in fallback** (``DEFAULT_PHRASES``) - applied only when no
   phrases come from YAML or request context, so the guard is never a
   no-op when enabled.

Other config:
    case_sensitive   bool, default False.
    allow_overrides  bool, default True. Set False to ignore request-time
                     phrases (lock the list to the policy).
"""
from __future__ import annotations

from typing import Any

from ..base import Guard, GuardCheckResult, GuardStage
from ..registry import register_guard

DEFAULT_PHRASES = [
    # generic prompt-injection red flags
    "ignore previous instructions",
    "system prompt",
    "developer mode",
]


class BannedSubstringsGuard(Guard):
    name = "banned-substrings"
    stage = GuardStage.INPUT
    description = (
        "Block text containing any configured banned phrase. Phrases come "
        "from policy YAML (`phrases:`) and/or request context "
        "(`context.banned_phrases`)."
    )

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)
        self.case_sensitive: bool = bool(config.get("case_sensitive", False))
        self.allow_overrides: bool = bool(config.get("allow_overrides", True))

        policy_phrases = config.get("phrases")
        if policy_phrases:
            self.policy_phrases: list[str] = [str(p) for p in policy_phrases]
        else:
            # No phrases configured in YAML -> use built-in fallback so the
            # guard is never silently a no-op when enabled.
            self.policy_phrases = list(DEFAULT_PHRASES)

    def _normalize(self, phrases: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for p in phrases:
            if not p:
                continue
            key = p if self.case_sensitive else p.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
        return out

    def _request_phrases(self, context: dict[str, Any] | None) -> list[str]:
        if not self.allow_overrides or not context:
            return []
        extras: list[str] = []
        for key in ("banned_phrases", "extra_banned_phrases"):
            val = context.get(key)
            if isinstance(val, str):
                extras.append(val)
            elif isinstance(val, list):
                extras.extend(str(x) for x in val)
        return extras

    async def check(
        self, text: str, *, context: dict[str, Any] | None = None
    ) -> GuardCheckResult:
        merged = self._normalize(self.policy_phrases + self._request_phrases(context))
        if not merged:
            return self._allow(text, metadata={"phrases_evaluated": 0})

        haystack = text if self.case_sensitive else text.lower()
        hits = [p for p in merged if p in haystack]
        if hits:
            return self._block(
                text,
                reasons=[f"banned phrase: {p!r}" for p in hits],
                categories=["policy.blocklist"],
                metadata={
                    "matches": hits,
                    "phrases_evaluated": len(merged),
                    "case_sensitive": self.case_sensitive,
                },
            )
        return self._allow(text, metadata={"phrases_evaluated": len(merged)})


register_guard("banned-substrings", lambda cfg: BannedSubstringsGuard(**cfg))
