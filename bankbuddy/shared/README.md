# shared/

The hinge of the codebase. Contains **only abstractions** - no vendor SDKs, no business logic.

## Contents

```text
bankbuddy_shared/
  contracts/      # Pydantic DTOs exchanged across services
  interfaces/     # Abstract Base Classes for every pluggable provider
```

## Why this exists

This package is the contract every service depends on. It implements the **Dependency Inversion Principle**: high-level modules (the API, the agent) depend on these abstractions, not on Microsoft Entra, Azure OpenAI, or LangGraph directly. Concrete implementations live inside each service.

| Interface | Purpose | Examples of concrete impls |
|-----------|---------|----------------------------|
| `IAuthProvider` | Authentication | Entra, Auth0, Cognito, Keycloak, LocalDev |
| `IAgentProvider` | Conversational agent | LangGraph, Foundry, OpenAI Assistants, Bedrock |
| `ILLMClient` | LLM chat / embed | LiteLLM (covers OpenAI, Azure, Bedrock, Ollama, vLLM) |
| `IBankingService` | Banking domain ops | MockBank, RealCoreBanking |
| `ISecretProvider` | Secret retrieval | env, Azure Key Vault, AWS Secrets Manager, Vault |
| `ISessionStore` | Session storage | Postgres, in-memory, Redis |
| `ITelemetry` | Observability | OTel, Noop |
| `IGuardrailPipeline` | AI guardrails (Phase 2) | Noop, NeMo, LLM Guard |

## Design principles applied

| Principle | Where |
|-----------|-------|
| Dependency Inversion (SOLID-D) | Every module under `interfaces/` |
| Interface Segregation (SOLID-I) | One ABC per concern; small method surface |
| Stable abstractions | This package has zero runtime dependencies beyond `pydantic` |

## How services consume it

Each service `Dockerfile` installs this package in editable mode:

```dockerfile
COPY shared /app/shared
RUN pip install -e /app/shared
```

That way, contracts and interfaces are imported as `from bankbuddy_shared.interfaces import IAuthProvider` everywhere.

## Adding a new interface

1. Create `interfaces/<concern>.py` with an `abc.ABC` class.
2. Re-export it from `interfaces/__init__.py`.
3. Keep it small - prefer many small interfaces over one big one.
4. Never import a vendor SDK here.
