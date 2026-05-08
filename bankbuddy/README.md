# BankBuddy

AI-powered retail banking assistant. Cloud-agnostic, container-based, interface-driven Python application.

> **Phase 1a (current):** project skeleton, interfaces, networks, and configuration.
> Subsequent phases add: mock bank, agent, API, UI, end-to-end run, and (Phase 2) guardrails.

---

## Architecture at a glance

```text
+---------------------------+        +-------------------------------------+
|       edge network        |        |          internal network           |
|  (host-published ports)   |        |     (NOT reachable from host)       |
|                           |        |                                     |
|   ui    :8080  --->host   |<------>|   api      (also on edge)           |
|                           |        |   agent    (internal only)          |
+---------------------------+        |   mock-bank(internal only)          |
                                     |   postgres (internal only)          |
                                     +-------------------------------------+
```

The `agent`, `mock-bank`, and `postgres` services have **no host port mapping**. The agent additionally requires a shared `X-Internal-Token` header that only the `api` service knows.

See [docs/security-boundaries.md](docs/security-boundaries.md) for the full network and trust model.

---

## Services

| Service       | Tech                              | Internal port | Host port | Public? |
|---------------|-----------------------------------|---------------|-----------|---------|
| `ui`          | FastAPI + React (Vite build)      | 8080          | 8080      | yes     |
| `api`         | FastAPI                           | 8000          | 8000      | yes (optional) |
| `agent`       | FastAPI + LangGraph               | 8100          | none      | **no**  |
| `mock-bank`   | FastAPI + SQLAlchemy              | 8200          | none      | **no**  |
| `postgres`    | postgres:16                       | 5432          | none      | **no**  |

---

## Design principles

This codebase is built on:

- **SOLID**, especially **Dependency Inversion**: every service depends on abstractions in [`shared/bankbuddy_shared/interfaces`](shared/bankbuddy_shared/interfaces), never on vendor SDKs directly.
- **Strategy + Factory**: the concrete provider (Entra vs LocalDev, LangGraph vs Foundry, Ollama vs Azure OpenAI) is selected by env var at startup.
- **Adapter**: vendor SDKs are wrapped behind interfaces.
- **Twelve-Factor**: all configuration is via environment variables; services are stateless; PostgreSQL holds state.
- **Defense in depth**: network isolation + internal token + per-schema DB users.

See [docs/design-principles.md](docs/design-principles.md) for the file-by-file mapping.

---

## Cloud portability

To migrate from Azure (Entra + Foundry + Azure OpenAI) to AWS, GCP, or fully on-prem, change env vars and (if a new provider is needed) add one class. See [docs/cloud-portability.md](docs/cloud-portability.md).

---

## Run locally

```powershell
copy .env.example .env
docker compose up --build
```

Then open http://localhost:8080.

> Phase 1a only delivers the skeleton. `docker compose up` will start `postgres` successfully; other services have placeholder containers until later phases.

---

## Repository layout

```text
bankbuddy/
  shared/                  # Interfaces (ABCs) + DTOs shared across services
  services/
    ui/                    # FastAPI + React
    api/                   # Public BFF, JWT, auth abstraction
    agent/                 # LangGraph orchestrator (internal only)
    mock-bank/             # Stub core-banking REST API
  infra/
    postgres/              # init.sql (schemas + per-service users)
    local/                 # docker-compose overrides
  docs/                    # architecture, principles, security, portability
  tests/                   # unit / integration / contract
```

---

## Phases

| Phase | Status | Deliverable |
|-------|--------|-------------|
| 1a    | **in progress** | Skeleton, interfaces, compose, configuration |
| 1b    | pending | Postgres + mock-bank with seeded data |
| 1c    | pending | Agent service (LangGraph + tools + Postgres checkpointer) |
| 1d    | pending | API service (auth abstraction + agent client) |
| 1e    | pending | UI service (FastAPI + React) |
| 1f    | pending | End-to-end run + smoke tests |
| 1g    | pending | All READMEs and docs finalized |
| 2     | future  | Guardrails (NeMo + LLM Guard + Presidio) behind `IGuardrailPipeline` |
