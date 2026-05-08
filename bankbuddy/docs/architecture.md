# Architecture

## Containers and trust boundaries

```text
                    +----------------------------+
   browser  ----->  |  ui  (8080)                |   edge network
                    |  FastAPI + React           |
                    +-------------+--------------+
                                  |
                                  v
                    +----------------------------+
                    |  api (8000)                |   edge + internal
                    |  FastAPI BFF, IAuthProvider|
                    +-------------+--------------+
                                  | X-Internal-Token
                                  v
                    +----------------------------+
                    |  agent (8100, internal)    |   internal network
                    |  LangGraph, IAgentProvider |
                    +------+-------------+-------+
                           |             |
              banking tools|             | LangGraph checkpoints
                           v             v
                +-------------------+  +------------------+
                | mock-bank (8200)  |  | postgres (5432)  |   internal
                | IBankingService   |  | app/agent/bank   |   network
                +-------------------+  +------------------+
```

- **edge** network: services with host port mappings (`ui`, optionally `api`).
- **internal** network: services with no host port mapping (`agent`, `mock-bank`, `postgres`).
- `api` is the only service joined to both networks.

## Request flow (Phase 1+)

1. User authenticates at the UI; UI calls `api` `/auth/*` endpoints.
2. `api` uses `IAuthProvider` (Entra / LocalDev / future) to verify and returns an app JWT.
3. UI calls `api` `POST /chat` with the JWT.
4. `api` verifies the JWT, builds an `AgentInvokeRequest`, and POSTs it to `agent` over the internal network with `X-Internal-Token`.
5. `agent` runs the LangGraph state machine. Tools call `mock-bank` (also internal). Memory is checkpointed in `agent_memory` schema in Postgres.
6. `agent` returns `AgentInvokeResponse` to `api`, which returns `ChatResponse` to UI.

## Why this split

| Concern | Service | Reason |
|---------|---------|--------|
| Presentation | `ui` | Can be replaced with a mobile/desktop client without backend changes |
| Authentication & authorization | `api` | Single trust boundary for tokens; UI never sees agent details |
| LLM orchestration | `agent` | Heavy deps (LangGraph, LiteLLM); kept off the public network |
| Domain data | `mock-bank` | Replaceable by a real core-banking system via `IBankingService` |
| State | `postgres` | Single managed dependency; per-schema isolation |

See [security-boundaries.md](security-boundaries.md), [design-principles.md](design-principles.md), and [cloud-portability.md](cloud-portability.md).
