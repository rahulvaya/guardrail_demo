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



# =============================================================================
# Default policy bundle (domain-neutral).
#
# Ships with the guardrails service as a sensible baseline for any
# consumer. Copy this file, change the `id`, and tune per your domain
# (e.g. retail-banking, hr-helpdesk, customer-support, fraud-ops).
#
# HOW TO ENABLE / DISABLE A GUARD
# -------------------------------
# Every guard below has `enabled: true|false`. Flip and recreate:
#
#     docker compose up -d --force-recreate guardrails
#
# Order matters per stage: cheap deterministic checks first, Azure
# managed checks next, custom scope/output checks last.
#
# Guard names must match the canonical hyphenated form registered in
# app/core/guards/*.py. Run
#   docker exec <container> python -c "from app.core import guards; \
#     from app.core.registry import registered_names; print(registered_names())"
# to list them.
# =============================================================================

id: default
description: Default domain-neutral policy. Safety + privacy + secret leak.

# ---------------------------------------------------------------------------
# Guard reference (G-id → registered guard name → purpose)
# Used by the comments below (e.g. "G-01 Content Filter") so the YAML lines
# up 1:1 with the architecture diagram.
#
#   G-01  Content Filter         azure-content-safety
#                                   (Analyze Text harm categories;
#                                    severity_threshold tunes block point)
#   G-02  Input Validation       token-limit            (DoS / oversize)
#                                prompt-injection       (heuristic jailbreak)
#                                azure-content-safety   (Prompt Shields on
#                                   input via enable_prompt_shield: true)
#   G-03  Intent Recognition     azure-topic-relevance  (Azure CS Custom
#                                   Categories; managed scope filter)
#                                topic-relevance        (local scope classifier;
#                                   supply `keywords:` to activate)
#   G-03a Task Adherence         azure-task-adherence   (Azure CS preview)
#   G-04  PII Filtering          azure-pii-detection    (Azure AI Language;
#                                   mode: block | sanitize;
#                                   needs AZURE_LANGUAGE_ENDPOINT + KEY)
#                                pii-detect             (Microsoft Presidio
#                                   https://github.com/microsoft/presidio;
#                                   regex fallback when presidio unavailable)
#                                output-pii-redact      (sanitize on output)
#   G-05  Sensitive Data Filter  secret-leak            (API keys, JWTs,
#                                   connection strings, private keys)
#   G-06  Copyright Detection    azure-content-safety   (Protected Material
#                                   on output via enable_protected_material)
#   G-07  Custom Blocklist       banned-substrings      (local phrase list;
#                                   driven by YAML + ${BANNED_PHRASES_JSON}
#                                   + per-request context.banned_phrases)
#                                azure-content-safety   (managed Text
#                                   Blocklists via blocklist_names: [...])
#   G-08  Hallucination Detection azure-groundedness    (Azure CS preview;
#                                   needs context.sources [+ .query for QnA])
#                                groundedness           (local overlap)
#   G-09  Bias Detection         bias-detect            (lexicon engine)
#   G-10  HITL Controls          *** not yet implemented — pending session
#                                   management standard (SGS-T03) ***
#   G-11  System Prompt Leakage  *** not yet a registered guard — planned ***
#                                   (segment hash-match + meta-instruction
#                                   classifier; see design doc §3 G-11)
#   G-12  Rate Limiting          *** enforced at APIM + FastAPI middleware —
#                                   not in this service's pipeline ***
#   G-13  Identity Context       *** FastAPI request middleware + system
#                                   prompt constructor — not in this service ***
#   G-14  RAG Pipeline Controls  *** ABAC filter at vector store retrieval —
#                                   not in this service ***
#   G-15  Schema Enforcement     schema-enforcement     (per-tool JSON Schema
#                                   validation of tool_input arguments and
#                                   tool_output payloads; blocks shape drift,
#                                   unknown fields, injection in typed fields)
#   G-16  Privilege-Scoped Output *** not yet implemented — requires G-13
#                                   guardrail-context envelope ***
#
# NOTE: sql-injection implements the SQL/injection sub-check of G-02.
# NOTE: G-10, G-11, G-12, G-13, G-14, G-16 are not YAML-pipeline guards;
#       they are enforced at APIM, middleware, or retrieval layers.
#
# Additional registered guards (not tied to a design-doc G-id):
#   competitor-mentions   block configured competitor names on output
#   toxicity              classifier-based toxicity score
#
# Run inside the container to list every registered name:
#   docker exec <container> python -c "from app.core import guards; \
#     from app.core.registry import registered_names; print(registered_names())"
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Pipeline checkpoints (architecture diagram ①…⑥):
#   ① api_input    — request entering the public chat API
#   ② input        — user message about to be sent to the LLM
#   ③ output       — LLM reply coming back to the orchestrator
#   ④ tool_input   — planned tool call (name + arguments) before execution
#   ⑤ tool_output  — JSON returned by a tool before it is fed back to the LLM
#   ⑥ api_output   — final assistant reply leaving the public API
#
# Every checkpoint is OPTIONAL: a missing top-level key means “no guards
# run at that checkpoint”. Callers may also send `stage=llm_input` /
# `stage=llm_output` as friendly aliases for `input` / `output`.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# ① API_INPUT  — first hop after the public chat API receives a request.
# Heaviest set: hostile traffic is rejected here before any LLM tokens burn.
# ---------------------------------------------------------------------------
api_input:
  - azure-content-safety:    # G-01 Content Filter + G-02 Prompt Shields
      enabled: true
      enable_harm_analysis: true   # G-01 Hate/SelfHarm/Sexual/Violence
      enable_prompt_shield: true   # G-02 direct + indirect injection
      enable_protected_material: false
      severity_threshold: 2
      fail_open: false       # trust boundary: a missing safety net = block
      timeout_seconds: 3
  - token-limit:             # G-02 Input Validation (DoS guard)
      enabled: true
      max_chars: 8000
  - prompt-injection:        # G-02 Input Validation (heuristic backup)
      enabled: true
  - azure-topic-relevance:   # G-03 Azure managed scope classifier
      # Managed counterpart to local `topic-relevance` below. Runs FIRST
      # so an Azure decision wins when available. Falls open on error /
      # 404 / unconfigured -> local `topic-relevance` provides backup.
      # Requires a Custom Category trained in your Content Safety
      # resource. Docs: https://learn.microsoft.com/azure/ai-services/content-safety/concepts/custom-categories
      enabled: false
      categories: []         # e.g. ["off_topic"]
      severity_threshold: 2
      fail_open: true
  - topic-relevance:         # G-03 Intent Recognition / local scope classifier
      enabled: false
      min_ratio: 0.05
      min_length: 20
  - azure-pii-detection:     # G-04 PII Filtering (Azure AI Language)
      enabled: false  # needs AZURE_LANGUAGE_ENDPOINT + AZURE_LANGUAGE_KEY
      mode: block
      min_confidence: 0.5
      fail_open: true
      # Whitelist: only flag real PII. Excludes DateTime / Person /
      # Organization / Quantity which cause false positives on benign text.
      categories: &pii_categories
        - USSocialSecurityNumber
        - CreditCardNumber
        - Email
        - PhoneNumber
        - Address
        - IPAddress
        - ABARoutingNumber
        - SWIFTCode
        - InternationalBankingAccountNumber
        - USDriversLicenseNumber
        - USIndividualTaxIdentification
  - banned-substrings:       # G-07 Custom Blocklist
      enabled: false
      case_sensitive: false
      allow_overrides: true
      phrases: []

  # ---- Reference entries: all remaining registered guards listed with
  # ---- enabled: false so this stage can serve as a copy-paste template.
  # ---- Most of these are output-side checks; flip on only if your
  # ---- threat model requires them on the public-API entry point.
  - pii-detect:              # G-04 PII Filtering — Microsoft Presidio (https://github.com/microsoft/presidio)
      enabled: true
      engine: presidio        # presidio | regex (regex is the offline fallback)
      mode: block             # block | sanitize
      # Presidio scores entities 0.0 – 1.0. 0.3 catches pattern-only SSNs
      # without context words; raise to 0.5 if you see false positives.
      min_score: 0.35
      spacy_model: en_core_web_sm
      # Whitelist Presidio entities so we focus on real PII and avoid the
      # noisy NLP-only types (PERSON / DATE_TIME / NRP / LOCATION).
      entities:
        - EMAIL_ADDRESS
        - PHONE_NUMBER
        - CREDIT_CARD
        - US_SSN
        - US_BANK_NUMBER
        - US_DRIVER_LICENSE
        - US_ITIN
        - US_PASSPORT
        - IBAN_CODE
        - IP_ADDRESS
        - CRYPTO
        - MEDICAL_LICENSE
  - output-pii-redact:       # output-only by design; off on input
      enabled: false
  - secret-leak:             # G-05 Sensitive Data Filter — reject leaked creds at the trust boundary
      enabled: true
  - azure-task-adherence:    # G-03a; runs at tool_input
      enabled: false
      # task_definitions:
      #   get_transactions: "Answer questions about account transactions only."
  - azure-groundedness:      # output-only (needs LLM reply + sources)
      enabled: false
  - competitor-mentions:     # usually output
      enabled: false
      # competitors: ["acme corp"]
  - toxicity:                # usually output; can pre-filter abusive prompts
      enabled: false
  - bias-detect:             # usually output
      enabled: false
      engine: lexicon
  - groundedness:            # output-only (needs reply + sources)
      enabled: false
  # G-02 Input Validation (SQL injection sub-check) — defense in depth at the public entrypoint.
  - sql-injection:
      enabled: true
  # G-15 Schema Enforcement — listed for completeness; arguments shape is
  # only meaningful at tool_input. Off at api_input.
  - schema-enforcement:
      enabled: false
      allow_unknown_tools: true

# ---------------------------------------------------------------------------
# ④ TOOL_INPUT  — planned tool call before execution. Validates the tool
# name + arguments the LLM produced; defends external APIs from injection
# and stops the agent from leaking PII / secrets into outbound calls.
# ---------------------------------------------------------------------------
tool_input:
  - token-limit:             # G-02 Input Validation (oversize args)
      enabled: true
      max_chars: 8000
  - azure-task-adherence:    # G-03a Task Adherence (vs user intent)
      enabled: true
      fail_open: true
      # Scope is defined entirely in policy — callers cannot override it.
      # Each tool is mapped to a free-text task definition that describes
      # the permitted scope of its response. Tools not listed are skipped.
      task_definitions:
        # --- Transaction retrieval ---
        get_transactions: >-
          Fetch and return raw transaction records for a single account or
          all accounts from the Open Finance API. Respond only with
          transaction data; do not offer analysis, advice, or commentary.
        fetch_transactions_for_period: >-
          Fetch and return merchant transaction records for a specific time
          period from the aic-data-service. Respond only with transaction
          data; do not offer analysis, advice, or commentary.
        # --- Cashflow and analysis ---
        detect_recurring_patterns: >-
          Analyse the most recently loaded transactions and return
          identified recurring debit, credit, and unclassified patterns.
          Respond only with pattern data derived from existing transaction
          state; do not fetch new data or provide financial advice.
        get_cashflow_report: >-
          Call the Finicity Cash Flow Business Report and return a compact
          summary of monthly inflow, outflow, net, and account balances.
          Respond only with cashflow summary data; do not provide
          investment or financial advice.
        # --- Financial statements ---
        get_report: >-
          Return the full unsummarized Finicity report including
          institutions, accounts, and raw analytics. Respond only with
          report data; do not interpret, summarize, or provide advice.
        get_categorized_expenses: >-
          Derive and return expense categories from transaction history,
          including per-category amount, percentage, monthly average, and
          trend. Respond only with categorized expense data.
        get_revenue_monthly: >-
          Return the monthly revenue series with growth percentages and
          total. Respond only with revenue data; do not provide forecasts
          or financial advice.
        extract_monthly_analytics_tool: >-
          Transform an existing report JSON and return inflow, outflow,
          and net series by period. Respond only with the derived analytics
          series; do not fetch new data or provide advice.
        # --- Receivables and payables ---
        get_receivables: >-
          Return outstanding receivables with aging buckets (on-time,
          15–30 days late, 30+ days late) and average days to pay.
          Respond only with receivables data; do not provide advice.
        get_payables: >-
          Return outstanding payables with aging buckets and average days
          to pay. Respond only with payables data; do not provide advice.
  - azure-pii-detection:     # G-04 PII Filtering on outbound params (Azure AI Language)
      enabled: false  # needs AZURE_LANGUAGE_ENDPOINT + AZURE_LANGUAGE_KEY
      mode: sanitize
      min_confidence: 0.5
      fail_open: true
      categories: *pii_categories
  - pii-detect:              # G-04 PII Filtering — Microsoft Presidio (regex fallback if presidio unavailable)
      enabled: true
      engine: presidio        # presidio | regex (regex is the offline fallback)
      mode: sanitize
      min_score: 0.3
      spacy_model: en_core_web_sm
      entities:
        - EMAIL_ADDRESS
        - PHONE_NUMBER
        - CREDIT_CARD
        - US_SSN
        - US_BANK_NUMBER
        - US_DRIVER_LICENSE
        - US_ITIN
        - US_PASSPORT
        - IBAN_CODE
        - IP_ADDRESS
        - CRYPTO
        - MEDICAL_LICENSE
  - secret-leak:             # G-05 Sensitive Data Filter
      enabled: true

  # ---- Reference entries: all remaining registered guards listed with
  # ---- enabled: false. Flip on per your tool surface.
  # harm categories on outbound tool args
  - azure-content-safety:
      enabled: false
      enable_harm_analysis: true
      enable_prompt_shield: false
      enable_protected_material: false
      severity_threshold: 2
      fail_open: true
      timeout_seconds: 3
  - azure-topic-relevance:   # scope check on tool args
      enabled: false
      categories: []
      severity_threshold: 2
      fail_open: true
  - azure-groundedness:      # output-only (needs sources)
      enabled: false
  - prompt-injection:        # block injection text in synthesised tool args
      enabled: false
  - output-pii-redact:       # output-only by design
      enabled: false
  - banned-substrings:
      enabled: false
      case_sensitive: false
      allow_overrides: true
      phrases: []
  - topic-relevance:         # local scope fallback
      enabled: false
      # keywords: ["account","balance","loan","card","atm"]
  - competitor-mentions:
      enabled: false
  - toxicity:
      enabled: false
  - bias-detect:
      enabled: false
      engine: lexicon
  - groundedness:            # output-only
      enabled: false
  # before any DB-backed tool call leaves the agent.
  - sql-injection:
      enabled: true
  # G-15 Schema Enforcement — validates `{tool, arguments}` payload
  # against per-tool JSON Schemas. Defaults to allow_unknown_tools=true
  # so the baseline policy is non-breaking; declare `schemas:` per tool
  # and set allow_unknown_tools=false to lock the surface down.
  - schema-enforcement:
      enabled: true
      strict: true
      allow_unknown_tools: true
      # schemas:
      #   get_transactions:
      #     input:
      #       type: object
      #       additionalProperties: false
      #       required: [account_id]
      #       properties:
      #         account_id: { type: string, pattern: "^[A-Za-z0-9_-]{1,32}$" }
      #         limit:      { type: integer, minimum: 1, maximum: 100 }

# ---------------------------------------------------------------------------
# ⑥ API_OUTPUT  — last hop before the response leaves the public API.
# Final-line PII / copyright / blocklist / bias sweep.
# ---------------------------------------------------------------------------
api_output:
  - azure-content-safety:    # G-01 Content Filter + G-06 Copyright Detection
      enabled: true
      enable_harm_analysis: true   # G-01 Hate/SelfHarm/Sexual/Violence
      enable_prompt_shield: false  # no injection check needed on final reply
      enable_protected_material: true   # G-06 Copyright Detection
      severity_threshold: 2
      fail_open: true        # don't lose a clean reply to a transient blip
      timeout_seconds: 3
      blocklist_names: []   # G-07 Custom Blocklist
  - azure-pii-detection:     # G-04 Last-line PII sweep (Azure AI Language)
      enabled: false  # needs AZURE_LANGUAGE_ENDPOINT + AZURE_LANGUAGE_KEY
      mode: sanitize
      min_confidence: 0.5
      fail_open: true
      categories: *pii_categories
  - pii-detect:              # G-04 PII Filtering — Microsoft Presidio (regex fallback if presidio unavailable)
      enabled: true
      engine: presidio        # presidio | regex (regex is the offline fallback)
      mode: sanitize
      min_score: 0.3
      spacy_model: en_core_web_sm
      entities:
        - EMAIL_ADDRESS
        - PHONE_NUMBER
        - CREDIT_CARD
        - US_SSN
        - US_BANK_NUMBER
        - US_DRIVER_LICENSE
        - US_ITIN
        - US_PASSPORT
        - IBAN_CODE
        - IP_ADDRESS
        - CRYPTO
        - MEDICAL_LICENSE
  - secret-leak:             # G-05 Last-line credential check
      enabled: true
  - banned-substrings:       # G-07 Custom blocklist on final reply
      enabled: false
      case_sensitive: false
      allow_overrides: true
      phrases: ${BANNED_PHRASES_JSON:[]}
  - bias-detect:             # G-09 Bias Detection
      enabled: true
      engine: lexicon

  # ---- Reference entries: all remaining registered guards listed with
  # ---- enabled: false so this stage can serve as a copy-paste template.
  - token-limit:             # reject oversized assistant replies
      enabled: false
      max_chars: 16000
  - prompt-injection:        # catch injection text echoed back
      enabled: false
  - output-pii-redact:       # token-level PII redactor
      enabled: false
  - azure-groundedness:      # requires context.sources [+ .query for QnA]
      enabled: false
      domain: Generic
      task: QnA
      require_sources: false
  - azure-task-adherence:    # requires policy task_definitions
      enabled: false
      # task_definitions:
      #   get_transactions: "Answer questions about account transactions only."
  # G-03 Domain Relevancy Check — design doc places G-03 at API Output as
  # the final output domain classification before delivery. Both guards are
  # disabled by default because they require domain-specific configuration
  # (trained Custom Category for azure-topic-relevance; keywords list for
  # topic-relevance). Supply keywords / categories for your domain to activate.
  - azure-topic-relevance:   # G-03 Azure managed scope classifier — enable with categories: [...]
      enabled: false
      categories: []         
      severity_threshold: 2
      fail_open: true
  - topic-relevance:         # G-03 local fallback — enable with keywords: [...]
      enabled: false
      # keywords: ["account","balance","loan","card","atm"]
  - competitor-mentions:
      enabled: false
      # competitors: ["acme corp"]
  - toxicity:
      enabled: false
  - groundedness:            # local overlap engine, no Azure dependency
      enabled: false
      engine: overlap
      block_threshold: 0.45
      warn_threshold: 0.65
      require_sources: false
  # G-02 Input Validation (SQL injection sub-check) — defense in depth on outbound replies; off by
  # default because legitimate assistant text may discuss SQL.
  - sql-injection:
      enabled: false
  # G-15 Schema Enforcement — output-shape only applies to tool payloads.
  - schema-enforcement:
      enabled: false
      allow_unknown_tools: true

# ---------------------------------------------------------------------------
# All registered guards are listed in EVERY stage below so operators can
# flip `enabled: true|false` per stage without editing the catalog. Guards
# that are not naturally meant for a given stage (e.g. groundedness on
# INPUT) are listed with `enabled: false` and a short note explaining why.
# Canonical guard names (must match registry):
#   azure-content-safety, azure-groundedness, azure-pii-detection,
#   azure-task-adherence, banned-substrings, bias-detect,
#   competitor-mentions, groundedness, output-pii-redact, pii-detect,
#   prompt-injection, schema-enforcement, secret-leak, sql-injection,
#   token-limit, topic-relevance, toxicity
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# INPUT pipeline (runs on every user message before the LLM)
# ---------------------------------------------------------------------------
input:
  # Cheap DoS guard. Reject obviously oversized inputs.
  - token-limit:
      enabled: true
      max_chars: 8000

  # Hard blocklist of phrases.
  # Sources (merged, case-insensitive, de-duped):
  #   1. `phrases:` list below (policy baseline). Supports env-var
  #      expansion: a whole-value `${VAR}` is JSON-parsed, so
  #      `phrases: ${BANNED_PHRASES_JSON}` with env
  #      BANNED_PHRASES_JSON='["foo","bar"]' becomes a real list.
  #      Use `${VAR:default}` to fall back when the env var is unset.
  #   2. per-request `context.banned_phrases` from /v1/check callers
  #   3. built-in DEFAULT_PHRASES (only if 1 and 2 are empty)
  # Set `allow_overrides: false` to lock the list to policy only.
  - banned-substrings:
      enabled: false
      case_sensitive: false
      allow_overrides: true
      phrases: []
      
  # Heuristic jailbreak / role-override detector (no GPU required).
  # Built-in PATTERNS catalog ships with ~10 patterns covering
  # role-override, system-prompt leakage, tool override, encoding tricks.
  # Override or extend per environment:
  #   patterns:             # REPLACES defaults; list of [regex, weight]
  #     - ["\\bdo anything now\\b", 0.95]
  #   extra_patterns:       # MERGED on top of active set
  #     - {pattern: "\\bprintenv\\b", weight: 0.6}
  - prompt-injection:
      enabled: true
      # block_threshold: 0.7

  # Microsoft Presidio PII detector. Falls back to regex if presidio-analyzer
  # is not installed. Presidio scores 0.0–1.0; raise min_score if noisy.
  - pii-detect:              # G-04 PII Filtering — Microsoft Presidio (regex fallback if presidio unavailable)
      enabled: true
      engine: presidio        # presidio | regex (regex is the offline fallback)
      mode: block
      min_score: 0.3
      spacy_model: en_core_web_sm
      entities:
        - EMAIL_ADDRESS
        - PHONE_NUMBER
        - CREDIT_CARD
        - US_SSN
        - US_BANK_NUMBER
        - US_DRIVER_LICENSE
        - US_ITIN
        - US_PASSPORT
        - IBAN_CODE
        - IP_ADDRESS
        - CRYPTO
        - MEDICAL_LICENSE

  # Output-only PII redactor. Listed for completeness; keep disabled on input.
  # Built-in masks: @card (preserves last 4), @iban (preserves first 4).
  # Custom example:
  #   extra_patterns:
  #     uk-sortcode: {regex: "\\b\\d{2}-\\d{2}-\\d{2}\\b", mask: "**-**-**"}
  - output-pii-redact:
      enabled: false

  # Refuse leaked secrets / API keys / connection strings in user input.
  # Default labels: aws-access-key, aws-secret, github-pat, openai-key,
  # private-key, jwt, bearer. Add your own:
  #   extra_patterns:
  #     acme-token: "\\bacme_[A-Za-z0-9]{32}\\b"
  #     stripe-key: "\\bsk_live_[A-Za-z0-9]{24,}\\b"
  - secret-leak:
      enabled: true

  # G-01 + G-02 — harm analysis (G-01) + Prompt Shields (G-02) run in
  # parallel. No Protected Material check needed on user messages.
  # fail_open: true because api_input already ran fail-closed.
  - azure-content-safety:
      enabled: true
      enable_harm_analysis: true   # G-01 Hate/SelfHarm/Sexual/Violence
      enable_prompt_shield: true   # G-02 direct + indirect injection
      enable_protected_material: false
      severity_threshold: 2
      fail_open: true
      timeout_seconds: 3
      # blocklist_names: ["corp-blocked-terms"]
      # halt_on_blocklist_hit: true

  # Azure AI Language PII detection. Higher-accuracy than Presidio but
  # requires AZURE_LANGUAGE_ENDPOINT + AZURE_LANGUAGE_KEY. Position it
  # before pii-detect so Azure runs first; Presidio acts as the fallback.
  - azure-pii-detection:
      enabled: false  # needs AZURE_LANGUAGE_ENDPOINT + AZURE_LANGUAGE_KEY
      mode: block
      min_confidence: 0.5
      fail_open: true
      categories: *pii_categories

  # Azure managed Groundedness — OUTPUT-only check. Disabled on input.
  - azure-groundedness:
      enabled: false

  # Azure managed Task Adherence — OUTPUT-only check. Disabled on input.
  - azure-task-adherence:
      enabled: false
      # task_definitions:
      #   get_transactions: "Answer questions about account transactions only."

  # Azure managed scope classifier. Runs first; local `topic-relevance`
  # below acts as fallback when Azure is unavailable / unconfigured.
  - azure-topic-relevance:
      enabled: false
      categories: []
      severity_threshold: 2
      fail_open: true

  # Topic relevance / scope. Opt in by supplying `keywords`.
  - topic-relevance:
      enabled: false
      # keywords: ["account","balance","loan","card","atm"]
      min_ratio: 0.05
      min_length: 20

  # Competitor mentions — typically an output concern, but can be applied
  # to input if you need to reject prompts that name competitors.
  - competitor-mentions:
      enabled: false
      # competitors: ["acme corp"]

  # Toxicity classifier — usually OUTPUT, but can pre-filter abusive input.
  - toxicity:
      enabled: false

  # Bias detection — usually OUTPUT, but can pre-filter loaded input.
  - bias-detect:
      enabled: false
      engine: lexicon

  # Groundedness — output-side check. Disabled on input.
  - groundedness:
      enabled: false

  # G-02 Input Validation (SQL injection sub-check) — block jailbreak prompts that ship raw SQL.
  - sql-injection:
      enabled: true

  # G-15 Schema Enforcement — tool-args concept, off on user input.
  - schema-enforcement:
      enabled: false
      allow_unknown_tools: true

# ---------------------------------------------------------------------------
# OUTPUT pipeline (runs on every assistant message before it leaves the agent)
# ---------------------------------------------------------------------------
output:
  # Reject oversized assistant replies.
  - token-limit:
      enabled: false
      max_chars: 16000

  # Hard blocklist of phrases in assistant output.
  - banned-substrings:
      enabled: false
      case_sensitive: false
      allow_overrides: true
      # phrases:
      #   - "confidential project x"

  # Prompt-injection patterns echoed back by the model.
  - prompt-injection:
      enabled: false

  # G-04 PII Filtering — design doc places G-04 at LLM Input (before the LLM
  # sees it) and API Output (final sweep), not at LLM Output. Disabled here
  # to avoid a redundant mid-pipeline PII pass; api_output catches it.
  - pii-detect:              # G-04 PII Filtering — Microsoft Presidio (regex fallback if presidio unavailable)
      enabled: false
      engine: presidio        # presidio | regex (regex is the offline fallback)
      mode: sanitize
      min_score: 0.3
      spacy_model: en_core_web_sm
      entities:
        - EMAIL_ADDRESS
        - PHONE_NUMBER
        - CREDIT_CARD
        - US_SSN
        - US_BANK_NUMBER
        - US_DRIVER_LICENSE
        - US_ITIN
        - US_PASSPORT
        - IBAN_CODE
        - IP_ADDRESS
        - CRYPTO
        - MEDICAL_LICENSE

  # Redact PII tokens in assistant replies (sanitize-style).
  - output-pii-redact:
      enabled: false

  # Refuse to leak secrets / API keys / connection strings.
  - secret-leak:
      enabled: true

  # G-01 + G-06 — harm analysis on LLM replies + Protected Material
  # copyright detection. No Prompt Shields needed on generated output.
  - azure-content-safety:
      enabled: true
      enable_harm_analysis: true   # G-01 Hate/SelfHarm/Sexual/Violence
      enable_prompt_shield: false  # not applicable on LLM output
      enable_protected_material: true   # G-06 Copyright Detection
      severity_threshold: 2
      fail_open: true
      timeout_seconds: 3
      # blocklist_names: ["corp-blocked-terms"]

  # Azure AI Language PII detection on assistant replies.
  - azure-pii-detection:
      enabled: false  # needs AZURE_LANGUAGE_ENDPOINT + AZURE_LANGUAGE_KEY
      mode: sanitize
      min_confidence: 0.5
      fail_open: true
      categories: *pii_categories

  # Azure managed Groundedness: requires `sources` in request context.
  - azure-groundedness:
      enabled: false
      domain: Generic
      task: QnA
      require_sources: false

  # Azure managed Task Adherence: scope defined in policy via task_definitions.
  - azure-task-adherence:
      enabled: false
      # task_definitions:
      #   get_transactions: "Answer questions about account transactions only."

  # Azure managed scope classifier on output (rare).
  - azure-topic-relevance:
      enabled: false
      categories: []
      severity_threshold: 2
      fail_open: true

  # Topic relevance on output (rare — most teams check input only).
  - topic-relevance:
      enabled: false
      # keywords: ["account","balance","loan","card","atm"]

  # Competitor mentions in assistant replies.
  - competitor-mentions:
      enabled: false
      # competitors: ["acme corp"]

  # Toxicity classifier on outgoing text.
  - toxicity:
      enabled: false

  # G-09 Bias Detection — design doc places this at API Output only, not LLM
  # Output. Disabled here; api_output runs the definitive bias sweep.
  - bias-detect:
      enabled: false
      engine: lexicon

  # G-08 Hallucination Detection — design doc places this at LLM Output.
  # Skips gracefully when context.sources is not supplied (require_sources:
  # false). Enable azure-groundedness below for the managed API variant.
  - groundedness:
      enabled: true
      engine: overlap
      block_threshold: 0.45
      warn_threshold: 0.65
      require_sources: false

  # G-02 Input Validation (SQL injection sub-check) — usually irrelevant on natural-language replies.
  - sql-injection:
      enabled: false

  # G-15 Schema Enforcement — tool-shape concept, off on free-text output.
  - schema-enforcement:
      enabled: false
      allow_unknown_tools: true

# ---------------------------------------------------------------------------
# TOOL_OUTPUT pipeline (runs on EVERY tool result before it is fed back to
# the LLM as a `role: "tool"` message). Defends against:
#   - prompt-injection text inside tool data
#   - PII leaking from downstream services
#   - oversized payloads blowing the LLM context window
#   - leaked secrets / API keys / connection strings
# ---------------------------------------------------------------------------
tool_output:
  - token-limit:
      enabled: true
      max_chars: 16000

  - banned-substrings:
      enabled: false

  - prompt-injection:
      enabled: true

  - pii-detect:              # G-04 PII Filtering — Microsoft Presidio (regex fallback if presidio unavailable)
      enabled: true
      engine: presidio        # presidio | regex (regex is the offline fallback)
      mode: sanitize
      min_score: 0.3
      spacy_model: en_core_web_sm
      entities:
        - EMAIL_ADDRESS
        - PHONE_NUMBER
        - CREDIT_CARD
        - US_SSN
        - US_BANK_NUMBER
        - US_DRIVER_LICENSE
        - US_ITIN
        - US_PASSPORT
        - IBAN_CODE
        - IP_ADDRESS
        - CRYPTO
        - MEDICAL_LICENSE

  - output-pii-redact:
      enabled: false

  - secret-leak:
      enabled: true

  # G-01 + G-02 — harm analysis (G-01) + Prompt Shields (G-02) to detect
  # injection content embedded in tool results before they reach the LLM.
  # Both calls run in parallel. No Protected Material needed on tool data.
  - azure-content-safety:
      enabled: true
      enable_harm_analysis: true   # G-01 Hate/SelfHarm/Sexual/Violence
      enable_prompt_shield: true   # G-02 indirect injection in tool data
      enable_protected_material: false
      severity_threshold: 2
      fail_open: true
      timeout_seconds: 3

  # Azure AI Language PII detection on tool results.
  - azure-pii-detection:
      enabled: false  # needs AZURE_LANGUAGE_ENDPOINT + AZURE_LANGUAGE_KEY
      mode: sanitize
      min_confidence: 0.5
      fail_open: true
      categories: *pii_categories

  # Azure managed Groundedness — typically output-only. Off here.
  - azure-groundedness:
      enabled: false

  # Azure managed Task Adherence — typically output-only. Off here.
  - azure-task-adherence:
      enabled: false
      # task_definitions:
      #   get_transactions: "Answer questions about account transactions only."

  - azure-topic-relevance:
      enabled: false
      categories: []
      severity_threshold: 2
      fail_open: true

  - topic-relevance:
      enabled: false

  - competitor-mentions:
      enabled: false

  - toxicity:
      enabled: false

  - bias-detect:
      enabled: false
      engine: lexicon

  - groundedness:
      enabled: false

  # G-02 Input Validation (SQL injection sub-check) — catch SQL fragments in
  # tool responses that could be reflected back into the LLM context.
  - sql-injection:
      enabled: true

  # G-15 Schema Enforcement — validates the tool's response payload
  # against the declared `output:` schema for the calling tool.
  - schema-enforcement:
      enabled: true
      strict: false              # log shape drift, do not block by default
      allow_unknown_tools: true

