"""Policy loader: parse YAML bundles into runtime guard pipelines."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# The copied guard implementations live under app.core (see Dockerfile).
from ..core.base import Guard, GuardStage
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
    input_specs = _normalize_specs(doc.get("input"))
    output_specs = _normalize_specs(doc.get("output"))
    tool_output_specs = _normalize_specs(doc.get("tool_output"))
    return Policy(
        id=pid,
        description=desc,
        input_specs=input_specs,
        output_specs=output_specs,
        tool_output_specs=tool_output_specs,
        raw=doc,
    )


def load_policies(policies_dir: str) -> dict[str, Policy]:
    """Load every *.yaml in the directory into a {id: Policy} map."""
    base = Path(policies_dir)
    if not base.exists():
        log.warning("policies_dir does not exist: %s", policies_dir)
        return {}

    policies: dict[str, Policy] = {}
    for path in sorted(base.glob("*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            log.error("failed to parse %s: %s", path.name, e)
            continue
        try:
            policy = _parse_policy(doc, path)
        except ValueError as e:
            log.error("invalid policy %s: %s", path.name, e)
            continue

        # Validate every guard name resolves so failures surface at boot,
        # not on the first request.
        all_names = (
            policy.input_guard_names
            + policy.output_guard_names
            + policy.tool_output_guard_names
        )
        unknown = [n for n in all_names if n not in registered_names()]
        if unknown:
            log.error("policy %s references unknown guards: %s", policy.id, unknown)
            continue

        policies[policy.id] = policy
        log.info(
            "loaded policy %s: input=%s output=%s tool_output=%s",
            policy.id,
            policy.input_guard_names,
            policy.output_guard_names,
            policy.tool_output_guard_names,
        )

    return policies


def build_pipeline(policy: Policy) -> GuardrailPipeline:
    """Materialize a `GuardrailPipeline` from a parsed policy."""
    input_guards: list[Guard] = []
    for name, cfg in policy.input_specs:
        # Per-guard env override of any policy field (rare; useful in dev).
        env_cfg = _env_override(name)
        merged = {**cfg, **env_cfg}
        g = build_guard(name, merged)
        if g.stage not in (GuardStage.INPUT, GuardStage.BOTH):
            log.warning("guard %s has stage=%s but is in INPUT block", name, g.stage)
        input_guards.append(g)

    output_guards: list[Guard] = []
    for name, cfg in policy.output_specs:
        env_cfg = _env_override(name)
        merged = {**cfg, **env_cfg}
        g = build_guard(name, merged)
        if g.stage not in (GuardStage.OUTPUT, GuardStage.BOTH):
            log.warning("guard %s has stage=%s but is in OUTPUT block", name, g.stage)
        output_guards.append(g)

    # Tool-output stage accepts any guard regardless of declared stage:
    # PII / prompt-injection / secret-leak guards designed for INPUT or
    # OUTPUT are equally valid against tool results, and operators may
    # legitimately want any of them here.
    tool_output_guards: list[Guard] = []
    for name, cfg in policy.tool_output_specs:
        env_cfg = _env_override(name)
        merged = {**cfg, **env_cfg}
        tool_output_guards.append(build_guard(name, merged))

    return GuardrailPipeline(
        input_guards=input_guards,
        output_guards=output_guards,
        tool_output_guards=tool_output_guards,
    )


def _env_override(guard_name: str) -> dict[str, Any]:
    """Read GUARD_<NAME>_CONFIG_OVERRIDE env (JSON) for ad-hoc tuning."""
    import json
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
