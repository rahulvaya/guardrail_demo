"""Policy loader: parse YAML bundles into runtime guard pipelines."""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# The copied guard implementations live under app.core (see Dockerfile).
from ..core.base import Guard, GuardStage, is_input_family, is_output_family
from ..core.pipeline import GuardrailPipeline
from ..core.registry import build_guard, registered_names

# Importing the guards package triggers self-registration of every guard
# under its canonical hyphenated name in the registry.
from ..core import guards as _guards  # noqa: F401  (side-effect)

log = logging.getLogger("guardrails.policy")


@dataclass
class Policy:
    id: str
    description: str
    input_specs: list[tuple[str, dict[str, Any]]]
    output_specs: list[tuple[str, dict[str, Any]]]
    tool_output_specs: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    api_input_specs: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    tool_input_specs: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    api_output_specs: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def input_guard_names(self) -> list[str]:
        return [n for n, _ in self.input_specs]

    @property
    def output_guard_names(self) -> list[str]:
        return [n for n, _ in self.output_specs]

    @property
    def tool_output_guard_names(self) -> list[str]:
        return [n for n, _ in self.tool_output_specs]

    @property
    def api_input_guard_names(self) -> list[str]:
        return [n for n, _ in self.api_input_specs]

    @property
    def tool_input_guard_names(self) -> list[str]:
        return [n for n, _ in self.tool_input_specs]

    @property
    def api_output_guard_names(self) -> list[str]:
        return [n for n, _ in self.api_output_specs]

    @property
    def all_guard_names(self) -> list[str]:
        return (
            self.api_input_guard_names
            + self.input_guard_names
            + self.tool_input_guard_names
            + self.output_guard_names
            + self.tool_output_guard_names
            + self.api_output_guard_names
        )


def _normalize_specs(items: list[Any] | None) -> list[tuple[str, dict[str, Any]]]:
    """Accept either ['name', ...] or [{'name': {...config}}, ...].

    A guard spec may include an `enabled: true|false` key (default: true).
    Disabled guards are dropped here so they never enter the pipeline.
    The `enabled` key is stripped from the config passed to the guard.
    """
    if not items:
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    for item in items:
        if isinstance(item, str):
            out.append((item, {}))
            continue
        if isinstance(item, dict):
            if len(item) != 1:
                raise ValueError(f"guard spec must have exactly one key, got: {item}")
            name, cfg = next(iter(item.items()))
            cfg = dict(cfg or {})
            enabled = cfg.pop("enabled", True)
            if not enabled:
                log.info("guard %s disabled by policy (enabled: false)", name)
                continue
            out.append((name, cfg))
        else:
            raise ValueError(f"unsupported guard spec: {item!r}")
    return out


def _parse_policy(doc: dict[str, Any], path: Path) -> Policy:
    pid = doc.get("id") or path.stem
    desc = doc.get("description", "")
    return Policy(
        id=pid,
        description=desc,
        api_input_specs=_normalize_specs(doc.get("api_input")),
        input_specs=_normalize_specs(doc.get("input")),
        tool_input_specs=_normalize_specs(doc.get("tool_input")),
        output_specs=_normalize_specs(doc.get("output")),
        tool_output_specs=_normalize_specs(doc.get("tool_output")),
        api_output_specs=_normalize_specs(doc.get("api_output")),
        raw=doc,
    )


def load_policies(policies_dir: str) -> dict[str, Policy]:
    """Load every *.yaml in the directory (or directories) into a {id: Policy} map.

    `policies_dir` may be a single path or several paths joined by the OS
    path separator (`:` on POSIX, `;` on Windows) or by `,`. Later
    directories override earlier ones if they define the same policy id.
    This lets a consumer mount an additional directory (e.g.
    `/policies-extra`) alongside the image-baked `/app/app/policies`.
    """
    raw_parts: list[str] = []
    for sep in (os.pathsep, ","):
        if sep in policies_dir:
            raw_parts = [p for p in policies_dir.split(sep) if p.strip()]
            break
    if not raw_parts:
        raw_parts = [policies_dir]

    paths: list[Path] = []
    for p in raw_parts:
        path = Path(p.strip())
        if not path.exists():
            log.warning("policies_dir does not exist: %s", path)
            continue
        paths.append(path)
    if not paths:
        return {}

    yaml_files: list[Path] = []
    for base in paths:
        yaml_files.extend(sorted(base.glob("*.yaml")))

    policies: dict[str, Policy] = {}
    for path in yaml_files:
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            log.error("failed to parse %s: %s", path.name, e)
            continue
        doc = _expand_env(doc, source=path.name)
        try:
            policy = _parse_policy(doc, path)
        except ValueError as e:
            log.error("invalid policy %s: %s", path.name, e)
            continue

        # Validate every guard name resolves so failures surface at boot,
        # not on the first request.
        all_names = policy.all_guard_names
        unknown = [n for n in all_names if n not in registered_names()]
        if unknown:
            log.error("policy %s references unknown guards: %s", policy.id, unknown)
            continue

        policies[policy.id] = policy
        log.info(
            "loaded policy %s: api_input=%s input=%s tool_input=%s "
            "output=%s tool_output=%s api_output=%s",
            policy.id,
            policy.api_input_guard_names,
            policy.input_guard_names,
            policy.tool_input_guard_names,
            policy.output_guard_names,
            policy.tool_output_guard_names,
            policy.api_output_guard_names,
        )

    return policies


def build_pipeline(policy: Policy) -> GuardrailPipeline:
    """Materialize a `GuardrailPipeline` from a parsed policy."""

    def _build(
        specs: list[tuple[str, dict[str, Any]]],
        *,
        family: str,  # "input" | "output" | "any"
        block_label: str,
    ) -> list[Guard]:
        guards: list[Guard] = []
        for name, cfg in specs:
            env_cfg = _env_override(name)
            merged = {**cfg, **env_cfg}
            g = build_guard(name, merged)
            if family == "input" and not (is_input_family(g.stage) or g.stage == GuardStage.BOTH):
                log.warning("guard %s stage=%s in %s block (mismatch)", name, g.stage, block_label)
            elif family == "output" and not (is_output_family(g.stage) or g.stage == GuardStage.BOTH):
                log.warning("guard %s stage=%s in %s block (mismatch)", name, g.stage, block_label)
            guards.append(g)
        return guards

    return GuardrailPipeline(
        api_input_guards=_build(policy.api_input_specs, family="input", block_label="API_INPUT"),
        input_guards=_build(policy.input_specs, family="input", block_label="INPUT"),
        tool_input_guards=_build(policy.tool_input_specs, family="input", block_label="TOOL_INPUT"),
        output_guards=_build(policy.output_specs, family="output", block_label="OUTPUT"),
        # Tool-output stage accepts any guard regardless of declared stage.
        tool_output_guards=_build(policy.tool_output_specs, family="any", block_label="TOOL_OUTPUT"),
        api_output_guards=_build(policy.api_output_specs, family="output", block_label="API_OUTPUT"),
    )


def _env_override(guard_name: str) -> dict[str, Any]:
    """Read GUARD_<NAME>_CONFIG_OVERRIDE env (JSON) for ad-hoc tuning."""
    upper = guard_name.upper().replace("-", "_")
    raw = os.getenv(f"GUARD_{upper}_CONFIG_OVERRIDE")
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        log.warning("invalid JSON in GUARD_%s_CONFIG_OVERRIDE; ignoring", upper)
        return {}


# ---------------------------------------------------------------------------
# Env-var expansion for policy YAML
# ---------------------------------------------------------------------------
# Supports ``${VAR}`` and ``${VAR:default}`` anywhere in a string scalar.
# When the *entire* scalar is a single ``${VAR}`` reference, the resolved
# value is parsed as JSON if possible so list/dict/number/bool values
# round-trip with the right type. Example::
#
#     phrases: ${BANNED_PHRASES_JSON}       # env: '["foo","bar"]' -> list
#     api_key: ${AZURE_CONTENT_SAFETY_KEY}  # env: 'abc'           -> str
#     min_confidence: ${PII_MIN_CONF:0.5}   # env unset            -> "0.5" -> 0.5
# ---------------------------------------------------------------------------

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")
_ENV_FULL_PATTERN = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}$")


def _coerce_scalar(value: str) -> Any:
    """Try JSON-parse so '["a","b"]' -> list, '0.5' -> float, 'true' -> bool."""
    try:
        return json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return value


def _expand_env(node: Any, *, source: str = "") -> Any:
    if isinstance(node, dict):
        return {k: _expand_env(v, source=source) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand_env(v, source=source) for v in node]
    if not isinstance(node, str):
        return node

    # Whole-string single-var reference -> type-preserving substitution.
    full = _ENV_FULL_PATTERN.match(node)
    if full:
        var, default = full.group(1), full.group(2)
        raw = os.environ.get(var)
        if raw is None:
            if default is None:
                log.warning(
                    "policy %s references unset env var ${%s}; leaving as empty string",
                    source, var,
                )
                return ""
            raw = default
        return _coerce_scalar(raw)

    # Partial substitution inside a larger string -> always string result.
    def _sub(match: re.Match[str]) -> str:
        var, default = match.group(1), match.group(2)
        raw = os.environ.get(var)
        if raw is None:
            if default is None:
                log.warning(
                    "policy %s references unset env var ${%s}; substituting empty",
                    source, var,
                )
                return ""
            return default
        return raw

    return _ENV_PATTERN.sub(_sub, node)


def validate_request_overrides(
    overrides: dict[str, dict[str, Any]],
    policy: Policy,
    allowed_keys: set[str],
    forbidden_keys: set[str],
) -> list[str]:
    """Validate per-request overrides against allowlist + policy guards.

    Returns a list of human-readable error messages. Empty list = valid.
    """
    errors: list[str] = []
    known_guards = set(policy.all_guard_names)
    for guard_name, cfg in overrides.items():
        if not isinstance(cfg, dict):
            errors.append(f"overrides[{guard_name!r}] must be an object, got {type(cfg).__name__}")
            continue
        if guard_name not in known_guards:
            errors.append(
                f"overrides[{guard_name!r}]: guard not present in policy "
                f"{policy.id!r} (known: {sorted(known_guards)})"
            )
            continue
        for key in cfg.keys():
            if key in forbidden_keys:
                errors.append(
                    f"overrides[{guard_name!r}].{key}: key is forbidden by "
                    f"GUARDRAILS_FORBIDDEN_OVERRIDE_KEYS (security boundary)"
                )
            elif key not in allowed_keys:
                errors.append(
                    f"overrides[{guard_name!r}].{key}: key is not in "
                    f"GUARDRAILS_OVERRIDABLE_KEYS allowlist (allowed: {sorted(allowed_keys)})"
                )
    return errors


def build_pipeline_with_overrides(
    policy: Policy,
    overrides: dict[str, dict[str, Any]],
) -> GuardrailPipeline:
    """Build a pipeline applying per-guard overrides on top of policy YAML.

    Assumes overrides have already been validated against the allowlist.
    Merge precedence (low -> high): policy YAML -> env override -> request override.
    """
    def _merge(specs: list[tuple[str, dict[str, Any]]]) -> list[Guard]:
        guards: list[Guard] = []
        for name, cfg in specs:
            env_cfg = _env_override(name)
            req_cfg = overrides.get(name, {})
            merged = {**cfg, **env_cfg, **req_cfg}
            guards.append(build_guard(name, merged))
        return guards

    return GuardrailPipeline(
        api_input_guards=_merge(policy.api_input_specs),
        input_guards=_merge(policy.input_specs),
        tool_input_guards=_merge(policy.tool_input_specs),
        output_guards=_merge(policy.output_specs),
        tool_output_guards=_merge(policy.tool_output_specs),
        api_output_guards=_merge(policy.api_output_specs),
    )
