"""Abstract Base Classes for every pluggable provider in BankBuddy.

Every concrete provider (Entra, LangGraph, LiteLLM, MockBank, etc.) MUST
implement one of these ABCs. Application code depends only on these
abstractions, satisfying the Dependency Inversion Principle.
"""
from .auth import IAuthProvider, AuthError
from .agent import IAgentProvider, AgentError
from .llm import ILLMClient, LLMError
from .banking import IBankingService, BankingError
from .secrets import ISecretProvider
from .session import ISessionStore
from .telemetry import ITelemetry
from .guardrails import IGuardrailPipeline, GuardrailDecision

__all__ = [
    "IAuthProvider",
    "AuthError",
    "IAgentProvider",
    "AgentError",
    "ILLMClient",
    "LLMError",
    "IBankingService",
    "BankingError",
    "ISecretProvider",
    "ISessionStore",
    "ITelemetry",
    "IGuardrailPipeline",
    "GuardrailDecision",
]
