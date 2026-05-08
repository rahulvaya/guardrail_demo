# Design principles

This document maps each principle/pattern to the file(s) that embody it. Updated as each phase lands.

## SOLID

| Principle | How it's applied | Files |
|-----------|------------------|-------|
| **S**ingle Responsibility | Four services, one job each: UI presents, API authenticates and routes, Agent orchestrates, MockBank stores domain data | `services/*` |
| **O**pen/Closed | Adding a new auth/agent/LLM provider means adding a class and registering it in the factory. Existing code never changes | `services/api/app/auth/factory.py`, `services/agent/app/providers/factory.py` (Phase 1c/1d) |
| **L**iskov Substitution | Every concrete provider is a drop-in for its ABC. Contract tests in `tests/contract/` enforce this | `tests/contract/` (Phase 1f) |
| **I**nterface Segregation | One small ABC per concern (auth, agent, LLM, banking, secrets, session, telemetry, guardrails) | `shared/bankbuddy_shared/interfaces/*.py` |
| **D**ependency Inversion | Services depend on `shared.interfaces`, not on Microsoft / AWS / Google SDKs | All `shared/bankbuddy_shared/interfaces/*.py` |

## Design patterns

| Pattern | Where | Why |
|---------|-------|-----|
| **Strategy** | `IAuthProvider`, `IAgentProvider`, `ILLMClient`, `IBankingService` | Runtime selection by env var (`AUTH_PROVIDER`, `AGENT_PROVIDER`, `LLM_PROVIDER`, `BANKING_BACKEND`) |
| **Factory Method** | `AuthProviderFactory.create()`, `AgentProviderFactory.create()` (Phase 1c/1d) | Decide which strategy to instantiate |
| **Adapter** | `EntraAuthProvider`, `LangGraphAgentProvider`, `LiteLLMClient` | Wrap vendor SDKs to satisfy our ABCs |
| **Facade** | `services/api/app/routers/chat.py` | Hide agent complexity behind a simple REST surface |
| **Repository** | `IBankingService` -> `MockBankHttpClient` | Swap mock for real core banking |
| **Layered architecture** | UI -> API -> Agent -> Tools -> Banking -> DB | Compile-time dependency direction is one-way |
| **Twelve-Factor** | All config via env vars, stateless services, Postgres for state | `.env.example`, `docker-compose.yml` |
| **Defense in depth** | Network isolation + internal token + per-schema DB users | `docker-compose.yml`, `infra/postgres/init.sql`, `services/agent/app/middleware/internal_token.py` (Phase 1c) |

## Phase-by-phase principle delivery

| Phase | Principle artifacts |
|-------|---------------------|
| 1a (current) | All ABCs in `shared/`; factories/concrete providers stubbed |
| 1b | Repository pattern in `mock-bank` |
| 1c | Strategy + Factory + Adapter for agent/LLM; defense-in-depth middleware |
| 1d | Strategy + Factory + Adapter for auth |
| 1e | Single-responsibility UI, runtime config |
| 1f | Liskov via contract tests |
| 2  | Strategy for `IGuardrailPipeline` |
