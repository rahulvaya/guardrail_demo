# Security boundaries

## Network isolation

`docker-compose.yml` defines two Docker bridge networks:

| Network    | Purpose | Services |
|------------|---------|----------|
| `edge`     | Reachable from the host / browser | `ui`, `api` |
| `internal` | NOT reachable from the host       | `api`, `agent`, `mock-bank`, `postgres` |

Only services on `edge` declare a `ports:` mapping. `agent`, `mock-bank`, and `postgres` are unreachable from outside Docker. `api` is the only service joined to both networks - it is the bridge.

## Internal-token guard (defense in depth)

Even within the internal network, every business endpoint on `agent` requires:

```
X-Internal-Token: <value of AGENT_INTERNAL_TOKEN>
```

Only the `api` service receives this env var. If a misconfigured route or attacker bypasses the network boundary, the token check still rejects them. The token must be rotated in production and stored via `ISecretProvider` (Key Vault / Secrets Manager / Vault) rather than `.env`.

## Database least privilege

`infra/postgres/init.sql` creates three roles, each owning one schema:

| Role         | Schema         | Used by      |
|--------------|----------------|--------------|
| `app_user`   | `app`          | `api`        |
| `agent_user` | `agent_memory` | `agent`      |
| `bank_user`  | `bank`         | `mock-bank`  |

A compromised service can only read/write its own schema's tables.

## Authentication abstraction

The UI never sees identity-provider tokens. The flow is:

1. UI redirects to `IAuthProvider.get_login_url()` (Entra today).
2. IdP redirects back with an authorization code.
3. `api` calls `IAuthProvider.exchange_code()` and issues an **app-signed JWT** (HS256, 1h TTL by default).
4. UI sends only the app JWT on subsequent calls.

Switching IdPs (Entra -> Cognito -> Keycloak) does not change the UI or the agent.

## Secrets

`ISecretProvider` abstracts secret retrieval. In Phase 1 the default is `EnvSecretProvider` reading `.env`. In production, set `SECRET_PROVIDER=azure-kv | aws-sm | vault` and provide the relevant config. Never bake secrets into images.

## What still needs hardening (later phases)

- TLS between services (mTLS via a service mesh, or sidecars).
- Egress restriction on the `internal` network (`internal: true`) - currently left open so the agent can reach external LLM APIs when configured.
- Rate limiting on `api`.
- WAF in front of `ui` / `api` (production).
- Guardrails (Phase 2).
