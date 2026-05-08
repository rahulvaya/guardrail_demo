"""Guardrail pipeline interface (Phase 2 will provide real implementations).

Implementations:
    - NoopGuardrails        (Phase 1 default; pass-through)
    - NeMoGuardrails        (Phase 2)
    - LLMGuardPipeline      (Phase 2)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class GuardrailDecision:
    allowed: bool
    sanitized_text: str
    reasons: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)


class IGuardrailPipeline(ABC):
    @abstractmethod
    async def check_input(self, text: str, *, context: dict | None = None) -> GuardrailDecision: ...

    @abstractmethod
    async def check_output(self, text: str, *, context: dict | None = None) -> GuardrailDecision: ...
