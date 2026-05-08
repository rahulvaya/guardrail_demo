"""Agent provider factory."""
from __future__ import annotations

from bankbuddy_shared.interfaces.agent import IAgentProvider
from bankbuddy_shared.interfaces.banking import IBankingService
from bankbuddy_shared.interfaces.llm import ILLMClient

from ..guardrails_client import RemoteGuardrailPipeline as GuardrailPipeline
from ..settings import Settings
from .langgraph_provider import LangGraphAgent
from .stubs import BedrockAgentProvider, FoundryAgentProvider, OpenAIAssistantProvider


def build_agent(
    settings: Settings,
    llm: ILLMClient,
    banking: IBankingService,
    guardrails: GuardrailPipeline | None = None,
) -> IAgentProvider:
    name = settings.agent_provider.lower()
    if name == "langgraph":
        return LangGraphAgent(
            llm=llm,
            banking=banking,
            guardrails=guardrails,
            block_message=settings.guardrails_block_message,
            system_prompt=settings.agent_system_prompt,
        )
    if name == "foundry":
        return FoundryAgentProvider()
    if name in ("openai-assistants", "openai"):
        return OpenAIAssistantProvider()
    if name == "bedrock":
        return BedrockAgentProvider()
    raise ValueError(f"unknown AGENT_PROVIDER: {settings.agent_provider}")
