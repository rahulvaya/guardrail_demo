"""LLM gateway adapters.

The agent depends on `ILLMClient` from `bankbuddy_shared`. The default
implementation routes through LiteLLM, which gives us a single API for
Ollama / OpenAI / Azure OpenAI / Bedrock / Vertex AI / vLLM.

Switching providers means changing `LLM_PROVIDER` + `LLM_MODEL` env vars.
No code changes anywhere else in the agent.
"""
