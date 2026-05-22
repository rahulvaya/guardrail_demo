"""sql-injection: block obvious SQL-injection patterns in text / tool args.

Defense-in-depth check that runs at any stage. Designed for the
``tool_input`` checkpoint where the LLM has synthesized arguments
for a database-backed tool (e.g. ``get_transactions(account_id=...)``),
but also useful on ``api_input`` to reject hostile prompts up front.

This is **pattern based** (not a SQL parser). It catches the well-known
shapes used by automated scanners and prompt-injected payloads:

* Inline comment markers (``--``, ``/* ... */``, ``#`` at line end)
* Stacked statements (``; SELECT``, ``; DROP``, ``; UPDATE`` ...)
* Tautologies (``OR 1=1``, ``OR 'a'='a'``)
* DML / DDL keywords in untrusted text (``DROP TABLE``, ``UNION SELECT``,
  ``INSERT INTO``, ``DELETE FROM``, ``UPDATE ... SET``, ``EXEC``,
  ``EXECUTE``, ``TRUNCATE``, ``ALTER TABLE``, ``CREATE TABLE``)
* Time-based blind probes (``SLEEP(``, ``WAITFOR DELAY``, ``BENCHMARK(``)
* OOB exfil probes (``LOAD_FILE``, ``INTO OUTFILE``, ``xp_cmdshell``,
  ``UTL_HTTP``)

YAML config (all optional)::

    sql-injection:
      enabled: true
      patterns:          # REPLACES defaults
        - "(?i)\\bdrop\\s+table\\b"
      extra_patterns:    # MERGED on top of active set
        - "(?i)\\bpg_sleep\\b"
      ignore_fields:     # JSON keys whose value is exempt (free-text fields)
        - "message"
        - "note"
      block_message: "request blocked: looks like SQL injection"
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

from ..base import Guard, GuardCheckResult, GuardStage
from ..registry import register_guard

DEFAULT_PATTERNS: list[str] = [
    # comment markers
    r"(?i)(^|\s)--\s",
    r"/\*.*?\*/",
    # stacked statements + DML/DDL keywords
    r"(?i);\s*(select|insert|update|delete|drop|alter|create|truncate|grant|revoke)\b",
    r"(?i)\bunion\s+(all\s+)?select\b",
    r"(?i)\bdrop\s+(table|database|schema|index)\b",
    r"(?i)\binsert\s+into\b",
    r"(?i)\bdelete\s+from\b",
    r"(?i)\bupdate\s+\w+\s+set\b",
    r"(?i)\btruncate\s+table\b",
    r"(?i)\balter\s+table\b",
    r"(?i)\bcreate\s+(table|database|user)\b",
    r"(?i)\bgrant\s+all\b",
    # exec / SP / OOB
    r"(?i)\bexec(ute)?\s*\(",
    r"(?i)\bxp_cmdshell\b",
    r"(?i)\butl_http\b",
    r"(?i)\bload_file\s*\(",
    r"(?i)\binto\s+outfile\b",
    # time-based blind
    r"(?i)\bsleep\s*\(",
    r"(?i)\bpg_sleep\s*\(",
    r"(?i)\bwaitfor\s+delay\b",
    r"(?i)\bbenchmark\s*\(",
    # tautologies
    r"(?i)\bor\s+1\s*=\s*1\b",
    r"(?i)\bor\s+'[^']+'\s*=\s*'[^']+'",
    r"(?i)\band\s+1\s*=\s*1\b",
]


def _compile(patterns: Iterable[str]) -> list[re.Pattern[str]]:
    out: list[re.Pattern[str]] = []
    for p in patterns:
        try:
            out.append(re.compile(p, re.DOTALL))
        except re.error:
            # Skip invalid pattern; do not raise (operator typo must not crash).
            continue
    return out


def _strip_ignored_fields(text: str, ignore_fields: set[str]) -> str:
    """Best-effort: if ``text`` is JSON, drop values for ``ignore_fields`` so
    free-form natural-language fields don't trigger false positives.
    Non-JSON text returns unchanged.
    """
    if not ignore_fields:
        return text
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text

    def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: ("" if k in ignore_fields else _walk(v)) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(x) for x in node]
        return node

    return json.dumps(_walk(obj))


class SqlInjectionGuard(Guard):
    name = "sql-injection"
    stage = GuardStage.BOTH  # safe at api_input AND tool_input
    description = (
        "Block text containing well-known SQL-injection patterns "
        "(comment markers, stacked DML/DDL, tautologies, time-based probes, "
        "OOB exfil). Pattern-based — not a full SQL parser."
    )

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)
        patterns = config.get("patterns") or DEFAULT_PATTERNS
        extra = config.get("extra_patterns") or []
        self._patterns = _compile(list(patterns) + list(extra))
        self._ignore_fields: set[str] = {str(f) for f in (config.get("ignore_fields") or [])}
        self._block_message: str = str(
            config.get("block_message", "request blocked by sql-injection guard")
        )

    async def check(
        self, text: str, *, context: dict[str, Any] | None = None
    ) -> GuardCheckResult:
        if not text:
            return self._allow(text, metadata={"patterns_evaluated": len(self._patterns)})

        haystack = _strip_ignored_fields(text, self._ignore_fields)
        hits: list[str] = []
        for pat in self._patterns:
            m = pat.search(haystack)
            if m:
                hits.append(m.group(0)[:80])
                if len(hits) >= 5:
                    break

        if hits:
            return self._block(
                text,
                reasons=[self._block_message, *[f"matched: {h!r}" for h in hits]],
                categories=["security.sql_injection"],
                metadata={
                    "matches": hits,
                    "patterns_evaluated": len(self._patterns),
                    "tool_name": (context or {}).get("tool_name"),
                },
            )
        return self._allow(text, metadata={"patterns_evaluated": len(self._patterns)})


register_guard("sql-injection", lambda cfg: SqlInjectionGuard(**cfg))
