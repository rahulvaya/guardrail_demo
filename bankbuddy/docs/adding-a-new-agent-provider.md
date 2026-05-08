# Adding a new agent provider

Every agent backend (LangGraph, Foundry, OpenAI Assistants, Bedrock Agents, Vertex Agents, ...) plugs in via the `IAgentProvider` interface.

## Steps

1. **Create the adapter**

   ```text
   services/agent/app/providers/<name>_provider.py
   ```

   Inherit from `bankbuddy_shared.interfaces.IAgentProvider` and implement `invoke()` and `stream()`. Translate the inbound `AgentInvokeRequest` into the vendor's API and translate the response back into `AgentInvokeResponse`.

2. **Register in the factory**

   In `services/agent/app/providers/factory.py` (Phase 1c), add a branch:

   ```python
   if name == "<name>":
       return MyNewProvider(settings)
   ```

3. **Document config**

   Add the new env vars to `.env.example` with sensible defaults and document them in `services/agent/README.md`.

4. **Add a contract test**

   `tests/contract/test_<name>_provider.py` should assert the provider satisfies `IAgentProvider` and returns valid `AgentInvokeResponse` objects (use a recorded fixture or a fake LLM).

5. **Switch via env**

   ```ini
   AGENT_PROVIDER=<name>
   ```

   Restart the agent container. No other service changes.

## Rules

- **No vendor SDK imports outside the adapter file.** The graph, tools, and tests must remain provider-agnostic.
- **Map errors to `AgentError`.** Callers should not have to catch vendor-specific exceptions.
- **Honor cancellation.** `invoke()` should respect `asyncio.CancelledError`.
- **Telemetry.** Use `ITelemetry.start_span("agent.invoke", provider="<name>")` so dashboards remain consistent.
