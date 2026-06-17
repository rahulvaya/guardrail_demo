# Local Setup

End-to-end instructions to run the BankBuddy demo (UI + API + Agent + mock-bank + Postgres + Guardrails) on a developer workstation using Docker Compose.

The repo contains two deployable units: x

| Unit | Path | Role |
|------|------|------|
| `guardrails-service` | [guardrails-service/](guardrails-service/) | Standalone FastAPI policy-enforcement service. Built into image `bankbuddy-guardrails:dev`. |
| `bankbuddy` | [bankbuddy/](bankbuddy/) | The reference banking app (UI, public API, LangGraph agent, mock core-banking, Postgres). Orchestrated via [bankbuddy/docker-compose.yml](bankbuddy/docker-compose.yml). |

The compose file in `bankbuddy/` builds **both** units (it points the `guardrails` service at `../guardrails-service`), so a single `docker compose up` brings the full stack up.

---

## 1. Prerequisites

| Tool | Version | Notes |
|------|---------|-------|
| Docker Desktop | 4.30+ (Compose v2) | Required. Linux containers. |
| Git | any recent | To clone. |
| PowerShell 7+ | | Examples below use `pwsh`. Bash works equivalently. |
| Python 3.11+ | optional | Only needed for running tests / smoke scripts outside containers. |
| Azure CLI | optional | Only if you want to mint AAD tokens for Azure-backed guardrails (Content Safety / Language / Azure OpenAI). |

LLM choice (pick one):

- **Ollama on the host** (default, fully offline) — install from https://ollama.com and run `ollama pull llama3.1:8b`.
- **Azure OpenAI** — requires an Azure subscription with a deployed chat model.

---

## 2. Clone and configure environment

```pwsh
git clone <this-repo-url> gaurdrails
cd gaurdrails\bankbuddy
copy .env.example .env
```

Open [bankbuddy/.env](bankbuddy/.env) and review. Defaults work out-of-the-box with Ollama; only the items below typically need attention.

### Required edits

| Variable | Why |
|----------|-----|
| `APP_JWT_SECRET` | Replace `change-me-please` with any random string. |
| `AGENT_INTERNAL_TOKEN` | Shared secret api ↔ agent. Run `python -c "import secrets; print(secrets.token_urlsafe(48))"` and paste. |
| `GUARDRAILS_INTERNAL_TOKEN` | Shared secret agent ↔ guardrails. Same generator as above. |

### Optional — host port overrides

If 8000 / 8080 / 5432 are taken on your machine, set in `.env`:

```dotenv
UI_PORT=8090
API_PORT=8001
POSTGRES_HOST_PORT=55434
```

The browser will then use http://localhost:8090 and `PUBLIC_API_BASE_URL=http://localhost:8001`.

### LLM provider

Default in `.env.example` is Ollama on the host:

```dotenv
LLM_PROVIDER=ollama
LLM_MODEL=llama3.1:8b
LLM_BASE_URL=http://host.docker.internal:11434
```

To use Azure OpenAI instead, set:

```dotenv
LLM_PROVIDER=azure
LLM_MODEL=gpt-4o-mini                # your deployment name
LLM_BASE_URL=https://<resource>.openai.azure.com/
LLM_API_VERSION=2024-08-01-preview
# Either an API key:
LLM_API_KEY=<key>
# Or leave LLM_API_KEY empty and supply a host-issued AAD token:
AZURE_OPENAI_AAD_TOKEN=<bearer-token>
```

`tools/refresh-aad-token.ps1` (under `bankbuddy/tools/`) refreshes the bearer token from your signed-in Azure CLI session.

### Azure-backed guardrails (optional)

The default policy [bankbuddy/policies/bankbuddy-default.yaml](bankbuddy/policies/bankbuddy-default.yaml) calls `azure-content-safety` and `azure-pii-detection`. To run them, populate in `.env`:

```dotenv
AZURE_CONTENT_SAFETY_ENDPOINT=https://<resource>.cognitiveservices.azure.com/
AZURE_LANGUAGE_ENDPOINT=https://<resource>.cognitiveservices.azure.com/
# Either keys:
AZURE_CONTENT_SAFETY_KEY=<key>
AZURE_LANGUAGE_KEY=<key>
# Or a pre-fetched AAD token (scope: https://cognitiveservices.azure.com/.default):
AZURE_CONTENT_SAFETY_AAD_TOKEN=<bearer>
AZURE_LANGUAGE_AAD_TOKEN=<bearer>
```

Without those, every Azure-backed guard fails open (logs a warning and allows traffic). The local-only guards (`token-limit`, `banned-substrings`, etc.) keep working.

---

## 3. Start the stack

From the `bankbuddy/` directory:

```pwsh
docker compose up --build
```

First build takes a few minutes (Python images for 5 services). Subsequent runs are cached.

Watch for:

```
bankbuddy-postgres   | database system is ready to accept connections
bankbuddy-guardrails | Uvicorn running on http://0.0.0.0:8001
bankbuddy-mock-bank  | Uvicorn running on http://0.0.0.0:8200
bankbuddy-agent      | Uvicorn running on http://0.0.0.0:8100
bankbuddy-api        | Uvicorn running on http://0.0.0.0:8000
bankbuddy-ui         | Uvicorn running on http://0.0.0.0:8080
```

Open the UI:

- http://localhost:8080  (or `UI_PORT` you chose)

Sign in with the local-dev provider (any username/password is accepted in `AUTH_PROVIDER=local-dev`).

---

## 4. Verify

### Health checks

```pwsh
curl http://localhost:8000/healthz       # api
curl http://localhost:8080/healthz       # ui
docker exec bankbuddy-guardrails curl -s http://localhost:8001/healthz
```

### Guardrails smoke test

```pwsh
$tok = (Get-Content bankbuddy/.env | Select-String '^GUARDRAILS_INTERNAL_TOKEN=').ToString().Split('=',2)[1]
docker exec bankbuddy-guardrails python -c @"
import os, httpx
h={'Authorization':'Bearer $tok','Content-Type':'application/json'}
r=httpx.post('http://localhost:8001/v1/check', headers=h,
  json={'policy_id':'bankbuddy-default','stage':'api_input','text':'Find ATMs near 10001.'})
print(r.status_code, r.json()['decision'])
"@
```

Expected: `200 allow`.

### End-to-end chat test

```pwsh
cd bankbuddy
pwsh tests\smoke_chat.ps1
```

---

## 5. API documentation (Swagger / OpenAPI)

Every service is built with FastAPI, so each one **automatically publishes** an interactive Swagger UI, a ReDoc page, and a raw OpenAPI JSON document. No code changes are needed — they ship by default.

| Service     | Swagger UI                 | ReDoc                       | OpenAPI JSON                       |
|-------------|----------------------------|-----------------------------|------------------------------------|
| `ui`        | http://localhost:8080/docs | http://localhost:8080/redoc | http://localhost:8080/openapi.json |
| `api`       | http://localhost:8000/docs | http://localhost:8000/redoc | http://localhost:8000/openapi.json |
| `guardrails`| http://localhost:8001/docs | http://localhost:8001/redoc | http://localhost:8001/openapi.json |
| `agent`     | http://localhost:8100/docs | http://localhost:8100/redoc | http://localhost:8100/openapi.json |
| `mock-bank` | http://localhost:8200/docs | http://localhost:8200/redoc | http://localhost:8200/openapi.json |

`ui` and `api` are reachable directly because they publish ports. `guardrails`, `agent`, and `mock-bank` live on the internal Docker network only — by design, since the agent must never be publicly reachable (see [bankbuddy/docs/security-boundaries.md](bankbuddy/docs/security-boundaries.md)).

### Option A — opt-in dev override (recommended)

A separate compose override [bankbuddy/docker-compose.dev.yml](bankbuddy/docker-compose.dev.yml) publishes the internal ports **only when you ask for it**:

```pwsh
cd bankbuddy
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

After that, all five Swagger URLs above are reachable from your browser. Stop with `Ctrl+C` or `docker compose down` and the dev ports go away — the base compose file is unchanged, so production posture is preserved.

If 8001 / 8100 / 8200 are taken, override in `.env`:

```dotenv
GUARDRAILS_HOST_PORT=18001
AGENT_HOST_PORT=18100
MOCK_BANK_HOST_PORT=18200
```

### Option B — peek without publishing ports

Use `docker exec` to hit the service from inside its own container:

```pwsh
docker exec bankbuddy-guardrails curl -s http://localhost:8001/openapi.json | python -m json.tool
docker exec bankbuddy-agent       curl -s http://localhost:8100/openapi.json | python -m json.tool
docker exec bankbuddy-mock-bank   curl -s http://localhost:8200/openapi.json | python -m json.tool
```

### Calling protected endpoints from Swagger UI

Some routes require a bearer token:

| Service     | Header                                     | Source |
|-------------|--------------------------------------------|--------|
| `api`       | `Authorization: Bearer <user-jwt>`         | Issued by `/auth/login` (the UI does this for you). |
| `agent`     | `X-Internal-Token: <AGENT_INTERNAL_TOKEN>` | From `bankbuddy/.env`. |
| `guardrails`| `Authorization: Bearer <GUARDRAILS_INTERNAL_TOKEN>` | From `bankbuddy/.env`. |

Click **Authorize** in Swagger UI and paste the value before invoking the protected operations.

### Production note

Do **not** ship the dev override or expose `/docs` publicly in production. Either keep the services internal (current posture) or set `docs_url=None, redoc_url=None, openapi_url=None` on the `FastAPI(...)` constructor in each service's `app/main.py` for the production build.

---

## 6. Common operations

### Reload guardrails policy after editing YAML

The compose file mounts `bankbuddy/policies/` into the guardrails container at `/policies-extra` (read-only). To pick up changes:

```pwsh
docker compose restart guardrails
```

### Tail logs

```pwsh
docker compose logs -f api agent guardrails
```

### Reset Postgres

```pwsh
docker compose down -v          # WARNING: drops the postgres-data volume
docker compose up --build
```

### Stop everything

```pwsh
docker compose down
```

---

## 7. Repository layout

```
gaurdrails/
├── guardrails-service/         # Standalone policy enforcement service
│   ├── app/                    # FastAPI app, guards, pipeline, registry
│   └── Dockerfile
├── bankbuddy/                  # Reference banking app
│   ├── docker-compose.yml      # ← run from here
│   ├── .env.example            # → copy to .env
│   ├── policies/
│   │   └── bankbuddy-default.yaml
│   ├── services/
│   │   ├── ui/                 # FastAPI + React build, port 8080
│   │   ├── api/                # Public BFF, port 8000
│   │   ├── agent/              # LangGraph orchestrator, internal only
│   │   └── mock-bank/          # Stub core-banking REST, internal only
│   ├── infra/postgres/init.sql # Schemas + per-service DB users
│   ├── shared/                 # Cross-service interfaces and DTOs
│   └── tests/                  # Smoke + guardrails tests
└── docs/                       # Architecture, design docs, diagrams
```

---

## 8. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `port is already allocated` on `docker compose up` | 8000 / 8080 / 5432 in use | Set `UI_PORT` / `API_PORT` / `POSTGRES_HOST_PORT` in `.env`. |
| UI loads but chat returns 401 | `AGENT_INTERNAL_TOKEN` mismatch between api and agent | Both read the same `.env`; ensure you didn't override one of them in a shell env. |
| Chat returns "I'm sorry — I can't help with that request" for benign input | Guardrails block. | `docker compose logs guardrails` and inspect `block_reasons`. Adjust [bankbuddy/policies/bankbuddy-default.yaml](bankbuddy/policies/bankbuddy-default.yaml) and `docker compose restart guardrails`. |
| Agent calls Azure OpenAI and gets `401`/`403` | AAD token expired | Re-run `bankbuddy/tools/refresh-aad-token.ps1` and update `AZURE_OPENAI_AAD_TOKEN` in `.env`, then `docker compose restart agent`. |
| Ollama: agent times out | Ollama not running on the host, or model not pulled | `ollama serve` and `ollama pull llama3.1:8b`. The agent reaches the host as `host.docker.internal:11434`. |
| `pydantic ... POSTGRES_PORT='tcp://...'` error in agent logs | Compose service-discovery env vars colliding with settings | Already fixed in source; ensure you're on the latest commit. |

For deeper architecture and security details, see [bankbuddy/docs/architecture.md](bankbuddy/docs/architecture.md), [bankbuddy/docs/security-boundaries.md](bankbuddy/docs/security-boundaries.md), and [bankbuddy/docs/guardrails.md](bankbuddy/docs/guardrails.md).
