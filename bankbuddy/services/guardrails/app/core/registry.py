"""Guard registry and pipeline builder.

Every built-in (and custom) guard registers itself here under a stable
name. Settings then drive which guards are *enabled*, their *order*,
and any *per-guard parameters* (thresholds, blocklists, etc.).

Configuration sources (in priority order, highest wins):

    1. Per-guard env flags:    GUARD_<NAME>_ENABLED=true|false
    2. Per-guard JSON config:  GUARD_<NAME>_CONFIG='{"threshold": 0.8}'
    3. Global toggle:          GUARDRAILS_ENABLED=true|false
    4. Defaults from the guard class
"""
from __future__ import annotations

import json
import logging
import os
from typing import Callable

from ..settings import Settings
from .base import Guard, GuardStage
from .pipeline import GuardrailPipeline

log = logging.getLogger("agent.guardrails.registry")

# name -> factory(config dict) -> Guard
_REGISTRY: dict[str, Callable[[dict], Guard]] = {}


def register_guard(name: str, factory: Callable[[dict], Guard]) -> None:
    """Register a guard factory under `name`. Idempotent."""
    if name in _REGISTRY:
        log.debug("guard %s already registered; overriding", name)
    _REGISTRY[name] = factory


def registered_names() -> list[str]:
    return sorted(_REGISTRY.keys())


def build_guard(name: str, config: dict | None = None) -> Guard:
    if name not in _REGISTRY:
        raise KeyError(f"unknown guard: {name}. Registered: {registered_names()}")
    return _REGISTRY[name](config or {})


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_json(key: str) -> dict:
    raw = os.getenv(key)
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        log.warning("guardrails: %s is not valid JSON; ignoring", key)
        return {}


def _is_enabled(guard_name: str, *, default: bool, master: bool) -> bool:
    if not master:
        return False
    upper = guard_name.upper().replace("-", "_")
    return _env_bool(f"GUARD_{upper}_ENABLED", default)


def _config_for(guard_name: str) -> dict:
    upper = guard_name.upper().replace("-", "_")
    return _env_json(f"GUARD_{upper}_CONFIG")


# ---------------------------------------------------------------------------
# Default order. Order matters: cheap deterministic checks run first so
# expensive ML-backed checks only see clean traffic.
# ---------------------------------------------------------------------------

DEFAULT_INPUT_ORDER: list[tuple[str, bool]] = [
    # (guard_name, default_enabled)
    # Azure AI Content Safety FIRST: a single managed call covers harm
    # categories (Hate/SelfHarm/Sexual/Violence) plus Prompt Shields
    # jailbreak / prompt-injection detection. All other guards default
    # OFF; opt back in via GUARD_<NAME>_ENABLED=true when needed.
    ("azure-content-safety", True),
    # Azure AI Language PII (SSN, credit card, email, phone, address...)
    # — Content Safety does NOT cover regex / entity PII.
    ("azure-pii-detection",  True),
    ("token-limit",          False),
    ("banned-substrings",    False),
    ("prompt-injection",     False),   # covered by Azure Prompt Shields
    ("pii-detect",           False),   # local regex fallback for PII
    ("banking-relevance",    False),
]

DEFAULT_OUTPUT_ORDER: list[tuple[str, bool]] = [
    ("azure-content-safety", True),    # harm / toxicity on the response
    ("azure-pii-detection",  True),    # redact PII in the model reply
    ("output-pii-redact",    False),
    ("secret-leak",          False),
    ("toxicity",             False),   # covered by Azure harm categories
    ("competitor-mentions",  False),
    # Custom RAI guards that fill gaps in the managed Azure stack.
    # Default OFF; flip on via GUARD_<NAME>_ENABLED=true.
    ("groundedness",         False),   # RAG hallucination (Azure preview)
    ("task-adherence",       False),   # runtime task-scope (Foundry eval-only)
    ("bias-detect",          False),   # stereotype patterns (gap in Hate/Unfair)
]


def build_pipeline_from_settings(settings: Settings) -> GuardrailPipeline:
    """Construct a `GuardrailPipeline` honoring environment overrides."""
    # Trigger registration of built-in guards.
    from . import guards as _guards  # noqa: F401  (side-effect: register)

    master = settings.guardrails_enabled

    input_guards: list[Guard] = []
    for name, default_on in DEFAULT_INPUT_ORDER:
        if not _is_enabled(name, default=default_on, master=master):
            log.info("guardrail %s [input] disabled", name)
            continue
        guard = build_guard(name, _config_for(name))
        if guard.stage not in (GuardStage.INPUT, GuardStage.BOTH):
            log.warning("guard %s declared stage=%s but is in INPUT order; running anyway",
                        name, guard.stage)
        input_guards.append(guard)
        log.info("guardrail %s [input] enabled config=%s", name, guard.config)

    output_guards: list[Guard] = []
    for name, default_on in DEFAULT_OUTPUT_ORDER:
        if not _is_enabled(name, default=default_on, master=master):
            log.info("guardrail %s [output] disabled", name)
            continue
        guard = build_guard(name, _config_for(name))
        if guard.stage not in (GuardStage.OUTPUT, GuardStage.BOTH):
            log.warning("guard %s declared stage=%s but is in OUTPUT order; running anyway",
                        name, guard.stage)
        output_guards.append(guard)
        log.info("guardrail %s [output] enabled config=%s", name, guard.config)

    if not master:
        log.warning("GUARDRAILS_ENABLED=false - all guards disabled (Phase 1 behavior)")

    return GuardrailPipeline(input_guards=input_guards, output_guards=output_guards)
