# services/api

Public Backend-for-Frontend (BFF). The **only** service the UI talks to, and the **only** service that can reach the internal `agent`.

## Status

**Phase 1a:** placeholder with `/health` and CORS configured. Auth providers and chat routing arrive in Phase 1d.

## Responsibilities

1. Authenticate users (via `IAuthProvider` -> Entra / LocalDev / future).
2. Issue and verify a short-lived app JWT.
3. Validate request shapes against `shared.contracts`.
4. Forward authorized chat requests to the agent service over the internal network with `X-Internal-Token`.
5. Persist user/session metadata in Postgres (`app` schema).

## Design principles

| Principle | Where (Phase 1d) |
|-----------|-----------------|
| Strategy + Factory | `app/auth/factory.py` selects `IAuthProvider` |
| Adapter | `entra_provider.py` wraps MSAL behind `IAuthProvider` |
| Facade | `routers/chat.py` exposes one POST endpoint hiding agent complexity |
| Single Responsibility | No business logic; just auth + routing |
| Defense in depth | Holds the only copy of `AGENT_INTERNAL_TOKEN` outside the agent itself |

## Configuration

| Env var | Purpose |
|---------|---------|
| `AUTH_PROVIDER` | `local-dev` (default) / `entra` / future |
| `ENTRA_*` | Entra app registration values |
| `APP_JWT_SECRET`, `APP_JWT_TTL_SECONDS` | App-issued JWT |
| `AGENT_INTERNAL_URL`, `AGENT_INTERNAL_TOKEN` | Internal hop to agent |
| `POSTGRES_*`, `APP_DB_USER`, `APP_DB_PASSWORD` | App DB |

## Switching auth provider (Entra -> AWS Cognito example)

1. Add `app/auth/cognito_provider.py` implementing `IAuthProvider`.
2. Register it in `app/auth/factory.py`.
3. Set `AUTH_PROVIDER=cognito` and the relevant env vars in `.env`.

No changes to UI or agent code.

See [docs/adding-a-new-auth-provider.md](../../docs/adding-a-new-auth-provider.md) and [docs/cloud-portability.md](../../docs/cloud-portability.md).
