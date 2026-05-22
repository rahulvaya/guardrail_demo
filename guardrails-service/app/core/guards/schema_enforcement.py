"""schema-enforcement: validate tool call shapes against a JSON Schema.

Runs at ``tool_input`` (LLM-planned arguments) and ``tool_output`` (raw
JSON returned by the tool). Blocks shape drift, hallucinated fields,
missing required params, and prompt-injection payloads stuffed into
fields that should have been numeric or enum-typed.

Schemas are declared in policy YAML keyed by tool name::

    schema-enforcement:
      enabled: true
      strict: true                     # fail on schema validation error
      allow_unknown_tools: false       # block if no schema for tool_name
      fail_open: false
      block_message: "tool call rejected by schema enforcement"
      schemas:
        get_accounts:
          input:
            type: object
            additionalProperties: false
            properties: {}
          output:
            type: object
        get_transactions:
          input:
            type: object
            additionalProperties: false
            required: [account_id]
            properties:
              account_id: { type: string, pattern: "^[A-Za-z0-9_-]{1,32}$" }
              limit:      { type: integer, minimum: 1, maximum: 100 }
          output:
            type: object

The guard expects ``context.tool_name`` and ``context.stage`` (set by the
pipeline). At ``tool_input`` the ``text`` is the JSON envelope
``{"tool": "...", "arguments": {...}}`` produced by the agent; the
guard pulls ``arguments`` out automatically. At ``tool_output`` the
``text`` is the raw tool JSON.

Falls back to ALLOW with a warning if jsonschema is not installed.
"""
from __future__ import annotations

import json
from typing import Any

from ..base import Guard, GuardCheckResult, GuardStage
from ..observability import obs_log
from ..registry import register_guard

try:
    import jsonschema
    from jsonschema import Draft7Validator
    _JSONSCHEMA_AVAILABLE = True
except ImportError:  # pragma: no cover
    _JSONSCHEMA_AVAILABLE = False


def _coerce_payload(text: str) -> Any:
    """Parse JSON; return raw string if not JSON."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


def _extract_arguments(payload: Any) -> Any:
    """When the agent sends `{"tool": "...", "arguments": {...}}`, unwrap
    to `arguments`. Otherwise return as-is."""
    if isinstance(payload, dict) and "tool" in payload and "arguments" in payload:
        return payload.get("arguments")
    return payload


class SchemaEnforcementGuard(Guard):
    name = "schema-enforcement"
    stage = GuardStage.BOTH  # registered on both tool_input and tool_output
    description = (
        "Validate tool call arguments and tool results against per-tool "
        "JSON Schemas. Blocks shape drift, hallucinated fields, and "
        "injection payloads in typed fields."
    )

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)
        self._strict: bool = bool(config.get("strict", True))
        self._allow_unknown: bool = bool(config.get("allow_unknown_tools", False))
        self._fail_open: bool = bool(config.get("fail_open", False))
        self._block_message: str = str(
            config.get("block_message", "tool call rejected by schema enforcement")
        )
        schemas_cfg = config.get("schemas") or {}
        self._validators: dict[str, dict[str, Any]] = {}
        if _JSONSCHEMA_AVAILABLE:
            for tool_name, pair in schemas_cfg.items():
                if not isinstance(pair, dict):
                    continue
                entry: dict[str, Any] = {}
                for side in ("input", "output"):
                    schema = pair.get(side)
                    if schema:
                        try:
                            Draft7Validator.check_schema(schema)
                            entry[side] = Draft7Validator(schema)
                        except jsonschema.SchemaError as e:
                            obs_log(
                                "schema_enforcement.invalid_schema",
                                level="warning",
                                tool=tool_name,
                                side=side,
                                error=str(e),
                            )
                if entry:
                    self._validators[tool_name] = entry
        elif schemas_cfg:
            obs_log(
                "schema_enforcement.jsonschema_missing",
                level="warning",
                hint="install jsonschema to enable schema enforcement",
            )

    def _which_side(self, context: dict[str, Any] | None) -> str:
        stage = (context or {}).get("stage", "")
        if isinstance(stage, str) and "output" in stage:
            return "output"
        return "input"

    async def check(
        self, text: str, *, context: dict[str, Any] | None = None
    ) -> GuardCheckResult:
        if not _JSONSCHEMA_AVAILABLE:
            return self._allow(text, metadata={"jsonschema": "missing"})

        tool_name = (context or {}).get("tool_name")
        if not tool_name:
            return self._allow(text, metadata={"reason": "no tool_name in context"})

        entry = self._validators.get(str(tool_name))
        side = self._which_side(context)

        if not entry or side not in entry:
            if self._allow_unknown:
                return self._allow(
                    text,
                    metadata={"tool": tool_name, "side": side, "schema": "absent"},
                )
            return self._block(
                text,
                reasons=[
                    self._block_message,
                    f"no {side} schema declared for tool {tool_name!r}",
                ],
                categories=["security.schema_unknown_tool"],
                metadata={"tool": tool_name, "side": side},
            )

        validator: Any = entry[side]
        payload = _coerce_payload(text)
        target = _extract_arguments(payload) if side == "input" else payload

        try:
            errors = sorted(validator.iter_errors(target), key=lambda e: list(e.path))
        except Exception as e:  # pragma: no cover  (validator should never raise)
            if self._fail_open:
                return self._allow(text, metadata={"validator_error": str(e)})
            return self._block(
                text,
                reasons=[self._block_message, f"validator error: {e}"],
                categories=["security.schema_validator_error"],
                metadata={"tool": tool_name, "side": side},
            )

        if not errors:
            return self._allow(
                text,
                metadata={"tool": tool_name, "side": side, "valid": True},
            )

        if not self._strict:
            return self._allow(
                text,
                metadata={
                    "tool": tool_name,
                    "side": side,
                    "valid": False,
                    "errors": [e.message for e in errors[:5]],
                },
            )

        return self._block(
            text,
            reasons=[
                self._block_message,
                *[f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errors[:5]],
            ],
            categories=["security.schema_violation"],
            metadata={
                "tool": tool_name,
                "side": side,
                "error_count": len(errors),
            },
        )


register_guard("schema-enforcement", lambda cfg: SchemaEnforcementGuard(**cfg))
