# services/agent

LangGraph-based conversational agent. **Internal-only service** - never reachable from the host or browser.

> **Security note:** this service has **no port mapping** in `docker-compose.yml` and lives on the `internal` Docker network. Even within that network, every business endpoint requires an `X-Internal-Token` header matching `AGENT_INTERNAL_TOKEN`. Only the `api` service knows that token.

## Status

**Phase 1a:** placeholder. Health endpoint plus a token-guarded `/internal/ping` to demonstrate the auth model. Real LangGraph orchestration arrives in Phase 1c.

## Scenarios it will handle (Phase 1c)

All seven banking scenarios. The graph chooses tools by name:

| Scenario | Tool |
|----------|------|
| Account inquiry | `get_accounts` |
| Transaction history | `get_transactions` |
| Fund transfer | `transfer_funds` |
| Loan eligibility | `check_loan_eligibility` |
| Card services | `block_card` |
| ATM locator | `find_atms` |
| FAQ | (LLM only, no tool) |

## Architecture (Phase 1c)

```text
internal HTTP --[X-Internal-Token check]--> AgentProviderFactory
                                                |
                                                v
                                  LangGraphAgentProvider
                                                |
              +--------+----------------+-------+--------+
              |        |                |                |
              v        v                v                v
         LiteLLM   Postgres      MockBankHttp       Telemetry
        (ILLMClient) checkpointer (IBankingService) (ITelemetry)
```

## Design principles

| Principle | Where |
|-----------|-------|
| Dependency Inversion | All deps via `shared.interfaces` |
| Strategy + Factory | `providers/factory.py` selects `IAgentProvider` from `AGENT_PROVIDER` env |
| Adapter | `langgraph_provider.py` wraps LangGraph SDK behind `IAgentProvider` |
| Defense in depth | Network isolation + internal-token middleware + per-schema DB user |

## Configuration

| Env var | Default | Purpose |
|---------|---------|---------|
| `AGENT_PROVIDER` | `langgraph` | Provider selector |
| `AGENT_INTERNAL_TOKEN` | (required) | Shared secret with `api` |
| `LLM_PROVIDER` | `ollama` | Routes via LiteLLM |
| `LLM_MODEL` | `llama3.1:8b` | |
| `LLM_BASE_URL` | `http://host.docker.internal:11434` | |
| `MOCK_BANK_URL` | `http://mock-bank:8200` | |
| `POSTGRES_*`, `AGENT_DB_USER`, `AGENT_DB_PASSWORD` | | LangGraph checkpointer |
| `AGENT_CHECKPOINTER` | `postgres` | `postgres` or `memory` |

## Adding a new agent provider

1. Add `app/providers/<name>_provider.py` implementing `IAgentProvider`.
2. Register it in `app/providers/factory.py`.
3. Set `AGENT_PROVIDER=<name>` in `.env`. No other code changes.

See [docs/adding-a-new-agent-provider.md](../../docs/adding-a-new-agent-provider.md).

## Cloud portability

| Concern | Today | Swap by |
|---------|-------|---------|
| Agent runtime | LangGraph in-process | Implement another `IAgentProvider` (Foundry / Bedrock / OpenAI Assistants) |
| LLM | LiteLLM -> Ollama | Change `LLM_PROVIDER` env var |
| Memory | Postgres checkpointer | Change `AGENT_CHECKPOINTER`; or implement Redis/DynamoDB checkpointer |
| Banking backend | mock-bank | Implement `IBankingService` against a real core |
