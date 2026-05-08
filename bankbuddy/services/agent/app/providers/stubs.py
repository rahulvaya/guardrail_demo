"""Stubs for non-default agent providers.

These exist so the factory can resolve any configured `AGENT_PROVIDER` to a
class. Each raises `NotImplementedError` from `invoke`. Replace with real
adapters following [docs/adding-a-new-agent-provider.md](../../../../docs/adding-a-new-agent-provider.md).

Vendor SDK imports must stay inside the relevant provider file. They MUST
NOT leak into the graph, tools, or shared package.
"""
from __future__ import annotations

from typing import AsyncIterator

from bankbuddy_shared.contracts.agent import AgentInvokeRequest, AgentInvokeResponse
from bankbuddy_shared.interfaces.agent import IAgentProvider


class _StubProvider(IAgentProvider):
    name = "stub"

    async def invoke(self, request: AgentInvokeRequest) -> AgentInvokeResponse:  # noqa: ARG002
        raise NotImplementedError(
            f"{self.name} provider is a stub in Phase 1. "
            "See docs/adding-a-new-agent-provider.md."
        )

    async def stream(self, request: AgentInvokeRequest) -> AsyncIterator[str]:  # noqa: ARG002
        raise NotImplementedError(
            f"{self.name} provider is a stub in Phase 1."
        )
        yield ""  # unreachable; satisfies the AsyncIterator return type


class FoundryAgentProvider(_StubProvider):
    name = "foundry"


class OpenAIAssistantProvider(_StubProvider):
    name = "openai-assistants"


class BedrockAgentProvider(_StubProvider):
    name = "bedrock"
