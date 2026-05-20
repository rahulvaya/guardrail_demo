# Guardrails Service

A standalone HTTP service that evaluates user input and assistant output
against a configurable pipeline of safety, privacy, and policy guards.
Consumers in any language call ``POST /v1/check``; they never embed
guard code, manage provider credentials, or upgrade guards themselves.

This service is **domain-neutral**. It ships with a generic `default`
policy and a library of safety / privacy guards. Each consumer picks
which guards to enable, tunes them via YAML, and can add custom
checks (regex rules or small Python guards) without changing service
code. A working example consumer — the BankBuddy retail-banking
demo — lives in [`../bankbuddy/`](../bankbuddy/).

## Repository layout

```text
guardrails-service/           # the server (this folder)
  app/
    main.py                   # FastAPI app, /healthz /readyz /v1/*
    auth.py                   # bearer-token (AAD JWT support comes next)
    settings.py               # env-driven config
    api/                      # v1 wire contract (CheckRequest / CheckResponse)
    core/                     # guard framework + built-in guards
    policies/                 # YAML policy bundles + loader
  Dockerfile
  requirements.txt
```

## At a glance

```
Consumer (any agent)             Guardrails Service              Providers
────────────────────             ──────────────────              ─────────
client.check_api_input(text)  ─► POST /v1/check  ──────────► Azure AI Content Safety
client.check_input(text)      ─► POST /v1/check                  Azure AI Language (PII)
client.check_tool_input(json) ─► POST /v1/check                  Azure AI Groundedness
client.check_tool_output(json)─► POST /v1/check                  Azure AI Task Adherence
client.check_output(text)     ─► POST /v1/check                  Presidio / regex
client.check_api_output(text) ─► POST /v1/check  ◄──── {decision, sanitized_text, reasons}
```

Up to **six** calls per turn (heavy checks on the trust boundary, lighter
checks deeper in the agent loop). All stages are optional — a stage with
zero enabled guards short-circuits in the consumer SDK without a network
hop. The full checkpoint layout is:

| # | `stage` value | Where it runs | Typical guards |
|---|---|---|---|
| ① | `api_input` | Edge — raw user payload just arrived | content-safety, prompt-shield, PII, token-limit |
| ② | `input` (alias `llm_input`) | Just before the LLM | banned-substrings, topic-relevance, prompt-injection |
| ④ | `tool_input` | Before each tool call (planned `{tool, arguments}`) | PII, secrets, oversize, task-adherence |
| ⑤ | `tool_output` | After each tool result, before feeding LLM | PII, secrets, prompt-injection, content-safety |
| ③ | `output` (alias `llm_output`) | LLM final answer | groundedness, task-adherence, bias, PII redact |
| ⑥ | `api_output` | Last sweep before client | content-safety, PII redact, secret-leak, bias |

A consumer that only needs the old 2-call model can leave the other four
stages empty in YAML — the service stays backwards-compatible.

## Prerequisites

What you need **before** building, running, or deploying this service.

### 1. Tooling

| Tool | Version | Used for |
|---|---|---|
| Docker (Desktop or Engine) | 24+ | Build and run the container |
| Docker Compose | v2 (bundled with Desktop) | Run the BankBuddy demo stack |
| Python | 3.11+ | Local non-container dev only |
| Azure CLI (`az`) | 2.60+ | Creating Azure resources, role assignments |
| `kubectl` + `helm` | latest | AKS deployment only |
| Bash / PowerShell | any recent | Smoke tests |

### 2. Azure resources

Provision once per environment (dev / stg / prod):

| Resource | Purpose | SKU guidance |
|---|---|---|
| **Azure AI Content Safety** account | Used by the `azure-content-safety` guard (Hate / Self-harm / Sexual / Violence / Prompt-shield) | `S0` |
| **Azure AI Language** account (Text Analytics) | Used by the `azure-pii-detection` guard | `S` |
| **Azure Key Vault** | Stores `GUARDRAILS_INTERNAL_TOKEN`, fallback Azure AI keys, any AAD client secrets | Standard |
| **Azure Container Registry** (AKS) | Hosts the `guardrails-service` image | Standard |
| **AKS cluster** with **OIDC issuer + Workload Identity** enabled (AKS only) | Pod federation to AAD | — |
| **User-assigned Managed Identity** (one for the guardrails pod) | Pod calls Azure AI without keys | — |
| **Microsoft Entra ID App Registration** (recommended for AAD auth mode) | Audience for consumer JWTs (`api://guardrails-service`) | — |

Content Safety and Language can live in the **same** Cognitive Services account
(multi-service) or separate accounts — the service treats them independently.

### 3. Identity model & role assignments

The service has **two trust boundaries**:

```
        consumer agent                  guardrails pod                Azure AI
        ─────────────                   ──────────────                ────────
   (a) Workload Identity / SP  ──JWT──► [validate JWT]
                                        [load policy + run guards]
                                        [acquire AAD token]   ─────► Content Safety
   (b)                                  Workload Identity ─────────► Language (PII)
```

#### (a) Consumer → Guardrails (caller identity)

| `GUARDRAILS_AUTH_MODE` | What the consumer needs | What you assign |
|---|---|---|
| `static` (dev only) | The shared `GUARDRAILS_INTERNAL_TOKEN` | — (just rotate the token) |
| `aad` (recommended) | A **Workload Identity** (AKS) or **Service Principal** (other hosts) federated to its own App Registration | Grant API permission on the **guardrails App Registration** to each consumer App Registration |
| `static_or_aad` (default) | Either of the above | Both / migration path |

Optional hard tenant isolation: set `GUARDRAILS_AAD_ALLOWED_APPIDS` to a CSV
of consumer App Registration object IDs. Anything else in the tenant is rejected
with HTTP 401 even if the JWT validates.

#### (b) Guardrails → Azure AI (provider identity)

The guardrails pod calls Azure AI Content Safety + Azure AI Language.
Pick **one** of the following:

| Option | Where it works | Setup |
|---|---|---|
| **Workload Identity → User-assigned MI** *(preferred for AKS)* | AKS w/ OIDC issuer | Federate the MI to the pod's `ServiceAccount`, mount via the workload identity webhook |
| **System-assigned MI** | Azure container runtimes (App Service, Container Apps, VM) | Enable on the host resource |
| **Service Principal** (client id + secret/cert) | Anywhere | Set `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_CLIENT_SECRET` (or cert) on the pod |
| **API keys** *(fallback)* | Anywhere | Set `AZURE_CONTENT_SAFETY_KEY` / `AZURE_LANGUAGE_KEY` |

Whichever AAD-based identity you pick, it **must** have this role assignment
on each Azure AI resource it talks to:

| Principal | Scope | Role |
|---|---|---|
| Guardrails runtime identity (MI / SP / App Reg) | `Microsoft.CognitiveServices/accounts/<acs-name>` | **`Cognitive Services User`** |
| Same principal | `Microsoft.CognitiveServices/accounts/<language-name>` | **`Cognitive Services User`** |

Missing this role → `HTTP 401 PermissionDenied` from Azure, and (because
`azure-content-safety` is fail-closed at the input stage in the default policy)
**every input request blocks**.

Example with `az`:

```bash
PRINCIPAL_ID=<object-id-or-app-id-of-the-guardrails-identity>
az role assignment create \
  --assignee "$PRINCIPAL_ID" \
  --role "Cognitive Services User" \
  --scope "/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<acs-name>"

az role assignment create \
  --assignee "$PRINCIPAL_ID" \
  --role "Cognitive Services User" \
  --scope "/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<language-name>"
```

After granting roles, **restart the container** so a fresh AAD token is issued.

#### Other RBAC you may need

| Principal | Scope | Role | When |
|---|---|---|---|
| Guardrails MI | Key Vault | `Key Vault Secrets User` | If using KV CSI for tokens / fallback keys |
| ACR pull identity | ACR | `AcrPull` | AKS pulling the image |
| Deploy pipeline SP | Resource Group | `Contributor` (or scoped custom role) | CI/CD that runs `az` / `helm` |

### 4. Secrets & config

You need values for these before first run (template in
[`.env.example`](.env.example)):

| Variable | Required when | Where it comes from |
|---|---|---|
| `GUARDRAILS_INTERNAL_TOKEN` | `auth_mode` includes `static` | Generate a 32+ byte random string; store in Key Vault |
| `AZURE_CONTENT_SAFETY_ENDPOINT` | always | Azure portal → Content Safety resource → Keys & endpoint |
| `AZURE_CONTENT_SAFETY_KEY` | API-key mode (fallback) | Same page; **omit** when using MI/SP |
| `AZURE_LANGUAGE_ENDPOINT` | always | Azure portal → Language resource |
| `AZURE_LANGUAGE_KEY` | API-key mode (fallback) | Same page; **omit** when using MI/SP |
| `GUARDRAILS_AAD_TENANT_ID` | `auth_mode` includes `aad` | `az account show --query tenantId` |
| `GUARDRAILS_AAD_AUDIENCE` | `auth_mode` includes `aad` | The Application ID URI of the guardrails App Reg, e.g. `api://guardrails-service` |
| `GUARDRAILS_AAD_ALLOWED_APPIDS` | optional | CSV of consumer App Reg object IDs |

The Dockerfile bakes **no secrets**. All values come from env / Key Vault at
runtime.

### 5. Network

| Environment | Inbound | Outbound |
|---|---|---|
| Local dev | `8001/tcp` from host (or compose-internal only) | Internet to `*.cognitiveservices.azure.com` (or your private endpoint) |
| AKS prod | Internal LoadBalancer / Private Link only — **never public** | VNet egress to Azure AI Private Endpoints; AAD/JWKS endpoints |

`NetworkPolicy` recommendation in AKS: ingress only from namespaces labelled
`guardrails-consumer=true`. See [Deploying → Production AKS](#production--aks).

### 6. Pre-flight checklist

Before your first `docker compose up` or `helm install`:

- [ ] Content Safety + Language resources provisioned, endpoints copied
- [ ] Identity decided (MI / SP / API key) and credentials available
- [ ] `Cognitive Services User` granted to that identity on **both** resources
- [ ] `GUARDRAILS_INTERNAL_TOKEN` generated (rotate the default!)
- [ ] (AAD mode) App Registration created, audience configured, consumer apps granted
- [ ] `.env` populated from `.env.example` — no secrets committed
- [ ] (AKS) Workload Identity federated, ACR pull role assigned, NetworkPolicy applied

## API contract

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/healthz` | none | Liveness |
| `GET` | `/readyz` | none | Readiness (policies loaded) |
| `GET` | `/v1/policies` | bearer | List loaded policy bundles |
| `GET` | `/v1/policies/{id}` | bearer | Inspect one policy |
| `POST` | `/v1/check` | bearer | Run a stage of a policy on text |

Request:

```json
{
  "stage": "input",
  "text": "How do I check my balance?",
  "policy_id": "bankbuddy-default",
  "context": { "user_id": "u-123", "session_id": "s-abc" }
}
```

`stage` accepts any of: `api_input`, `input`, `llm_input` (alias of
`input`), `tool_input`, `output`, `llm_output` (alias of `output`),
`tool_output`, `api_output`. Unknown stages return HTTP 422.

Response:

```json
{
  "decision": "allow",
  "sanitized_text": "How do I check my balance?",
  "stage": "input",
  "policy_id": "bankbuddy-default",
  "duration_ms": 42.7,
  "block_reasons": [],
  "block_categories": [],
  "guards": [ /* per-guard outcomes */ ],
  "request_id": "7c352584-503f-4ea8-9e59-35dbdef0a15d"
}
```

Caller behaviour:

| `decision` | Action |
|---|---|
| `allow` | Use `text` unchanged. Continue. |
| `sanitize` | Use `sanitized_text` instead. Continue. |
| `block` | Do not send `text` downstream. Show `block_reasons` or your own message. |

### Per-request overrides (optional)

The server-side YAML policy decides **which** guards run. Consumers may
optionally tune a whitelisted subset of fields on a single request via
the `overrides` field — useful when one caller (e.g. fraud-ops) needs a
different threshold than the default policy ships with:

```json
{
  "policy_id": "bankbuddy-default",
  "stage": "input",
  "text": "...",
  "overrides": {
    "azure-pii-detection":   { "min_confidence": 0.5 },
    "azure-content-safety":  { "severity_threshold": 4 }
  }
}
```

Server-controlled rules (configurable via env, see below):

| Setting | Default | Purpose |
|---|---|---|
| `GUARDRAILS_ALLOW_REQUEST_OVERRIDES` | `true` | Master switch. `false` rejects any `overrides` with HTTP 403. |
| `GUARDRAILS_OVERRIDABLE_KEYS` | `min_confidence,severity_threshold,max_chars,block_threshold,warn_threshold,min_ratio,min_length,mode` | CSV of guard-config keys consumers MAY tune. |
| `GUARDRAILS_FORBIDDEN_OVERRIDE_KEYS` | `enabled,fail_open,endpoint,api_key,aad_token,api_version,language` | CSV of keys NEVER overridable, even if added to the allowlist. Security boundary. |

Consumers **cannot** disable a guard, change credentials, flip a fail-open/closed
flag, or add a new guard via overrides. Anything outside the allowlist returns
HTTP 400 with the offending key in the response.

## Authentication

Every URL, secret, and tenant value is driven by environment variables
(see [`.env.example`](.env.example)). The service supports two bearer
mechanisms selectable via `GUARDRAILS_AUTH_MODE`:

| Mode | Behaviour |
|---|---|
| `static` | Validate `Authorization: Bearer <GUARDRAILS_INTERNAL_TOKEN>` only. |
| `aad` | Validate Microsoft Entra ID JWTs only (issuer + JWKS + audience + optional app-id allow-list). |
| `static_or_aad` *(default)* | Accept either - useful for migrations and dev. |

| Layer | Mechanism | Why |
|---|---|---|
| Network | AKS internal LoadBalancer / Private Link / VNet-only | Never publicly reachable |
| Caller identity (recommended) | **Workload Identity → AAD JWT**, validated via JWKS | Real identity, auto-rotated, no shared secrets |
| Caller identity (fallback) | Static bearer (`GUARDRAILS_INTERNAL_TOKEN`) | Dev / non-AAD callers |
| Authorization | `GUARDRAILS_AAD_ALLOWED_APPIDS` (CSV) restricts which app-ids may call `/v1/*` | Tenant isolation |
| Provider credentials | Workload Identity on the guardrails pod only | Azure AI keys never leave this service |

### Environment variables

| Variable | Default | Notes |
|---|---|---|
| `GUARDRAILS_AUTH_MODE` | `static_or_aad` | `static` / `aad` / `static_or_aad` |
| `GUARDRAILS_INTERNAL_TOKEN` | `please-rotate-this-token` | Required for `static` and `static_or_aad` |
| `GUARDRAILS_DEFAULT_POLICY_ID` | `bankbuddy-default` | Used when request omits `policy_id` |
| `GUARDRAILS_POLICIES_DIR` | `/app/app/policies` | Override to mount external policies |
| `GUARDRAILS_AAD_TENANT_ID` | *unset* | Required when AAD mode is enabled |
| `GUARDRAILS_AAD_AUDIENCE` | *unset* | E.g. `api://guardrails-service` |
| `GUARDRAILS_AAD_ALLOWED_APPIDS` | *unset* | CSV of caller app-ids; empty = any caller in the tenant |
| `GUARDRAILS_AAD_ISSUER` | derived from tenant | Override for sovereign clouds / testing |
| `GUARDRAILS_AAD_JWKS_URI` | derived via OIDC discovery | Override for sovereign clouds / testing |
| `AZURE_CONTENT_SAFETY_ENDPOINT` / `_KEY` | *unset* | Used by `azure-content-safety`, `azure-groundedness`, and `azure-task-adherence` guards |
| `AZURE_CONTENT_SAFETY_AAD_TOKEN` | *unset* | Optional pre-fetched AAD token (scope `https://cognitiveservices.azure.com/.default`); skips DefaultAzureCredential |
| `AZURE_LANGUAGE_ENDPOINT` / `_KEY` | *unset* | Used by the `azure-pii-detection` guard |
| `AZURE_LANGUAGE_AAD_TOKEN` | *unset* | Optional pre-fetched AAD token for Language |
| `BANNED_PHRASES_JSON` | *unset* | Example: JSON array string consumed by `banned-substrings` via `phrases: ${BANNED_PHRASES_JSON}` in YAML |

Copy [`.env.example`](.env.example) to `.env` and edit; the Dockerfile
does not bake any secrets.

### Azure role assignments (required)

If you use Entra ID tokens (Managed Identity / Workload Identity / Service Principal)
instead of API keys, the calling identity must have Azure RBAC data-plane access
to the Azure AI resource used by this service.

Minimum required role assignment on the Cognitive Services account referenced by
`AZURE_CONTENT_SAFETY_ENDPOINT` (and/or `AZURE_LANGUAGE_ENDPOINT`):

| Principal | Scope | Role |
|---|---|---|
| Guardrails runtime identity (MI/SP/App Registration) | `Microsoft.CognitiveServices/accounts/<resource-name>` | `Cognitive Services User` |

Notes:

- Missing RBAC produces `HTTP 401 PermissionDenied` from Azure APIs.
- In this repo policy, `azure-content-safety` for input is configured fail-closed,
  so RBAC/auth failures block the request.
- If you use separate resources for Content Safety and Language, assign the role
  on both resources.

Example (Azure CLI):

```bash
az role assignment create \
  --assignee <principal-object-id-or-app-id> \
  --role "Cognitive Services User" \
  --scope "/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<resource-name>"
```

After granting role assignments, restart/recreate the container so fresh tokens
are used.

### Enabling AAD on AKS

1. Create an App Registration for the service (the **audience**).
   Expose an API and grant access to the consumer App Registrations.
2. Federate each consumer's Workload Identity to its App Registration.
3. Set `GUARDRAILS_AUTH_MODE=aad`, `GUARDRAILS_AAD_TENANT_ID`,
   `GUARDRAILS_AAD_AUDIENCE` on the pod (e.g. via a Helm value or
   `ConfigMap`).
4. Optionally restrict to specific callers with
   `GUARDRAILS_AAD_ALLOWED_APPIDS`.
5. Consumers acquire tokens with `DefaultAzureCredential().get_token(<aad_scope>)`
   and send them in the `Authorization: Bearer <token>` header on every
   `POST /v1/check` request.

## Running locally

```bash
cd guardrails-service
docker build -t guardrails-service:dev .
docker run --rm -p 8001:8001 \
  -e GUARDRAILS_INTERNAL_TOKEN=dev-token-please-rotate \
  -e GUARDRAILS_DEFAULT_POLICY_ID=bankbuddy-default \
  -e AZURE_CONTENT_SAFETY_ENDPOINT=https://<your-acs>.cognitiveservices.azure.com \
  -e AZURE_CONTENT_SAFETY_KEY=<key> \
  -e AZURE_LANGUAGE_ENDPOINT=https://<your-language>.cognitiveservices.azure.com \
  -e AZURE_LANGUAGE_KEY=<key> \
  guardrails-service:dev

curl -s http://localhost:8001/healthz
curl -s http://localhost:8001/readyz
```

Or run the whole BankBuddy stack (postgres + mock-bank + agent + api + ui +
guardrails) via Docker Compose:

```bash
cd ../bankbuddy
cp .env.example .env   # edit secrets
docker compose up -d --build
```

In compose mode the guardrails service is reachable from sibling containers
as `http://guardrails:8001` and is **not** published to the host (defense-in-depth).

## Consuming the service

The service is a plain HTTP API — any language that can do `POST` with a
bearer header can use it. There is **no required SDK**. Reference clients
exist as conveniences only.

### 1. cURL (smoke test)

```bash
TOKEN=dev-token-please-rotate
curl -s http://localhost:8001/v1/check \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "stage": "input",
    "text": "How do I check my balance?",
    "policy_id": "bankbuddy-default",
    "context": {"user_id": "u-123", "session_id": "s-abc"}
  }'
```

Inspect available policies:

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8001/v1/policies
```

### 2. Python

A reference client lives at
[`bankbuddy/services/agent/app/guardrails_client.py`](../bankbuddy/services/agent/app/guardrails_client.py)
(httpx-based, supports static bearer + AAD via `DefaultAzureCredential`,
warmup, and per-request overrides). Copy it as-is or use this minimal
stdlib version:

```python
import json, urllib.request

GUARDRAILS_URL = "http://guardrails:8001"
TOKEN = "dev-token-please-rotate"

def check(stage: str, text: str, *, policy_id="bankbuddy-default",
          context=None, overrides=None) -> dict:
    body = {"stage": stage, "text": text, "policy_id": policy_id,
            "context": context or {}}
    if overrides:
        body["overrides"] = overrides
    req = urllib.request.Request(
        f"{GUARDRAILS_URL}/v1/check",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {TOKEN}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())

# Wire around your LLM call
def turn(user_text: str) -> str:
    pre = check("input", user_text)
    if pre["decision"] == "block":
        return "I'm sorry — I can't help with that request."
    safe_input = pre["sanitized_text"]

    raw_reply = call_llm(safe_input)   # your code

    post = check("output", raw_reply)
    if post["decision"] == "block":
        return "I'm sorry — I can't share that."
    return post["sanitized_text"]
```

For production Python services, prefer the `httpx`-based reference client
(connection pooling, async, AAD support) over `urllib`.

### 3. Java

Java 21 stdlib — no SDK required. Same wire contract:

```java
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.JsonNode;
import java.util.Map;

public class GuardrailsClient {
    private static final ObjectMapper M = new ObjectMapper();
    private final HttpClient http = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(2)).build();
    private final String baseUrl;
    private final String policyId;
    private final java.util.function.Supplier<String> tokenProvider;

    public GuardrailsClient(String baseUrl, String policyId,
                            java.util.function.Supplier<String> tokenProvider) {
        this.baseUrl = baseUrl;
        this.policyId = policyId;
        this.tokenProvider = tokenProvider;
    }

    public JsonNode check(String stage, String text,
                          Map<String, Object> context,
                          Map<String, Map<String, Object>> overrides) throws Exception {
        var body = new java.util.HashMap<String, Object>();
        body.put("stage", stage);
        body.put("text", text);
        body.put("policy_id", policyId);
        body.put("context", context == null ? Map.of() : context);
        if (overrides != null && !overrides.isEmpty()) body.put("overrides", overrides);

        var req = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl + "/v1/check"))
                .timeout(Duration.ofSeconds(5))
                .header("Authorization", "Bearer " + tokenProvider.get())
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(M.writeValueAsString(body)))
                .build();
        var resp = http.send(req, HttpResponse.BodyHandlers.ofString());
        if (resp.statusCode() >= 400) {
            throw new RuntimeException("guardrails " + resp.statusCode() + ": " + resp.body());
        }
        return M.readTree(resp.body());
    }
}
```

Acquire an AAD token in Java with the Azure SDK:

```java
// build.gradle: implementation 'com.azure:azure-identity:1.13.+'
import com.azure.identity.DefaultAzureCredentialBuilder;
import com.azure.core.credential.TokenRequestContext;

var cred = new DefaultAzureCredentialBuilder().build();
var ctx  = new TokenRequestContext().addScopes("api://guardrails-service/.default");

var client = new GuardrailsClient(
    "http://guardrails:8001",
    "bankbuddy-default",
    () -> cred.getToken(ctx).block().getToken()   // cache + refresh in production
);

var result = client.check(
    "input",
    "How do I check my balance?",
    Map.of("user_id", "u-123"),
    Map.of("azure-pii-detection", Map.of("min_confidence", 0.5))   // optional overrides
);

String decision = result.get("decision").asText();
String safeText = result.get("sanitized_text").asText();
```

For Spring Boot use `RestClient`/`WebClient` instead of `HttpClient`; the
JSON shape is identical.

### 4. Differences between Python and Java consumers

| Concern | Python (reference impl) | Java (stdlib / Spring) |
|---|---|---|
| Auth — static bearer | env var `GUARDRAILS_INTERNAL_TOKEN` | env var, `application.yml`, or Key Vault |
| Auth — Entra ID JWT | `azure.identity.DefaultAzureCredential().get_token(scope)` | `com.azure:azure-identity` `DefaultAzureCredentialBuilder` |
| HTTP client | `httpx.AsyncClient` (recommended) or `urllib` | `java.net.http.HttpClient` (Java 21+) or Spring `RestClient` |
| Token caching | The Azure SDK caches in-memory; reuse one credential | Cache `AccessToken` until ~5 min before `getExpiresAt()`; do **not** call `getToken()` per request |
| Async | `await client.check_input(text)` | `HttpClient.sendAsync(...)` returns `CompletableFuture` |
| Connection pooling | One `httpx.AsyncClient` per process | One `HttpClient` per process |
| JSON | dict / pydantic | Jackson `ObjectMapper` / records |

Both languages send the **identical** JSON payload to the **identical**
endpoint. There is no language-specific contract — only language-specific
ergonomics.

## Deploying

### Local — single container

See [Running locally](#running-locally) above.

### Local / dev — Docker Compose

The repo's [`bankbuddy/docker-compose.yml`](../bankbuddy/docker-compose.yml)
shows the canonical deployment shape:

```yaml
guardrails:
  build: ../guardrails-service
  container_name: bankbuddy-guardrails
  env_file: .env                      # GUARDRAILS_*, AZURE_* secrets
  networks: [internal]                # internal-only, no host port
  expose: ["8001"]
  healthcheck:
    test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8001/healthz')"]
    interval: 10s
    timeout: 3s
    retries: 5
```

### Production — AKS

Recommended shape:

| Concern | Choice |
|---|---|
| Workload | `Deployment`, min 2 replicas, `HPA` on CPU + p95 `duration_ms` |
| Service | `ClusterIP` (or internal `LoadBalancer` for cross-VNet) — **no public ingress** |
| Network | `NetworkPolicy` allowing only namespaces labelled `guardrails-consumer=true` |
| Auth | `GUARDRAILS_AUTH_MODE=aad` + AKS Workload Identity on consumers |
| Provider auth | Workload Identity on the **guardrails pod**; assign `Cognitive Services User` on the Azure AI resource (see [Azure role assignments](#azure-role-assignments-required)) |
| Secrets | Key Vault CSI driver (only fallback static tokens; AAD mode needs none) |
| Policies | `ConfigMap` mounted at `GUARDRAILS_POLICIES_DIR` — change YAML, `kubectl rollout restart` |
| Resilience | `PodDisruptionBudget` (`minAvailable: 1`), `topologySpreadConstraints` across zones |
| Observability | OpenTelemetry → Azure Monitor; emit `policy_id`, `decision`, `duration_ms`, per-guard outcomes; **never log raw text** |
| CI/CD | Build → ACR push → `helm upgrade --install` per env, with smoke test against `/v1/check` and a golden-prompt set before promotion |

A Helm chart and AKS Bicep are on the roadmap.

### Deploying only this service

If you already have an agent and just want to add guardrails:

1. `docker build -t <registry>/guardrails-service:<tag> guardrails-service/`
2. Push to ACR / your registry.
3. Run as a sidecar, separate Deployment, or standalone container — anywhere
   your agent can reach it over HTTP.
4. Set `GUARDRAILS_INTERNAL_TOKEN` (or AAD env) and the Azure AI provider
   creds. Mount your policy YAML.
5. Point the agent at `http://<host>:8001` and pass the token in
   `Authorization`.

Nothing in the agent's runtime changes besides two HTTP calls per turn.

## Consumer onboarding (any team)

1. Get a federated identity (AKS Workload Identity) for your service, or a
   shared bearer token from the guardrails platform team.
2. Get a `policy_id` allocated for your use case (e.g.
   `retail-banking-default`, `internal-helpdesk-strict`). Different needs →
   different policy bundle, **not** different request payload.
3. Implement up to six HTTP calls per LLM turn — one per active stage
   (`api_input`, `input`, `tool_input` for each tool call, `tool_output`
   for each tool result, `output`, `api_output`). Stages with no enabled
   guards short-circuit; the bare minimum is `input` + `output`.
   See the cURL / Python / Java examples above and the
   [`bankbuddy/services/agent/app/providers/langgraph_provider.py`](../bankbuddy/services/agent/app/providers/langgraph_provider.py)
   wiring for a worked 6-checkpoint integration.
4. Choose a `fail_mode` per stage in your client: **closed** for
   `api_input` / `input` / `tool_input` (block on guardrails-service
   unreachable), **open** for `api_output` / `tool_output` / `output`
   (allow on unreachable, log loudly).
5. (Optional) Pass `overrides` per request for whitelisted threshold tuning.
6. Forward `block_reasons` + `request_id` to your observability stack so
   every block is auditable end-to-end.

## Built-in guards

All guards register under canonical hyphenated names. Reference each in
policy YAML; flip `enabled: true|false` per stage. Each guard can run in
any of the six checkpoint stages — the **default stage** column below is
only the placement the shipped [`default.yaml`](app/policies/default.yaml)
uses; you are free to move or duplicate guards across stages.

The shipped [`default.yaml`](app/policies/default.yaml) starts with a
reference block that maps the OWASP-style G-IDs (G-01 … G-09) to the
specific guard implementations, so you only edit toggles, not the
catalog. Top-level YAML keys map to the `stage` values listed in
[API contract](#api-contract): `api_input`, `input`, `tool_input`,
`output`, `tool_output`, `api_output`.

### Local guards (no external dependency)

| Name | G-ID | Default stage(s) | Purpose |
|---|---|---|---|
| `token-limit` | G-02 | api_input, tool_input, tool_output | DoS guard. Reject oversized text by `max_chars`. |
| `banned-substrings` | G-07 | api_input, input | Hard blocklist. Phrases come from YAML `phrases:`, env (`${BANNED_PHRASES_JSON}`), or per-request `context.banned_phrases`. Options: `case_sensitive`, `allow_overrides`. |
| `prompt-injection` | G-02 | api_input, input, tool_output | Heuristic jailbreak / role-override detector. |
| `pii-detect` | G-04 | tool_input, tool_output, output, api_output | Regex PII (SSN, credit card, email, phone, IPv4, IBAN). `mode: block\|sanitize`. |
| `output-pii-redact` | G-04 | output, api_output | Sanitize-style redactor for assistant replies. |
| `secret-leak` | G-05 | tool_input, tool_output, output, api_output | Refuse API keys, connection strings, JWTs, private keys. |
| `topic-relevance` | G-03 | input | Scope check via `keywords:`. Also registered as alias `banking-relevance`. |
| `competitor-mentions` | — | output | Block configured competitor names. |
| `bias-detect` | G-09 | output, api_output | Lexicon-based stereotype / demographic-skew detector. |
| `groundedness` | G-08 | output | Local overlap-based hallucination check against `context.sources`. |
| `task-adherence` | G-03a | tool_input, output | Local heuristic that the reply / planned tool call stays on the configured task. |
| `toxicity` | G-01 | output | Classifier-based toxicity score. |

### Azure-managed guards

All Content-Safety-family guards share the same `AZURE_CONTENT_SAFETY_*`
credentials (endpoint + key or AAD). Azure AI Language guard uses
`AZURE_LANGUAGE_*`.

| Name | G-ID | Default stage(s) | Azure API | Notes |
|---|---|---|---|---|
| `azure-content-safety` | G-01 / G-02 / G-06 / G-07 | api_input, tool_output, api_output | `text:shieldPrompt`, `text:analyze`, `text:detectProtectedMaterial` | Harm categories + prompt-shield (`api_input`) + Protected Material (`api_output` via `enable_protected_material: true`) + optional Text Blocklists via `blocklist_names: [...]` (+ `halt_on_blocklist_hit`). |
| `azure-pii-detection` | G-04 | api_input, tool_input, tool_output, output, api_output | Azure AI Language PII | `mode: block\|sanitize`, `min_confidence`. |
| `azure-groundedness` | G-08 | output | `text:detectGroundedness` | Requires `context.sources` (and `context.query` for QnA `task`). Config: `domain`, `task`, `require_sources`. |
| `azure-task-adherence` | G-03a | tool_input, output | `text:detectTaskAdherence` | Requires `context.task_definition` (or `system_prompt`). Config: `require_task_definition`. |

Every Azure guard supports `fail_open: true|false` (allow vs block on
API errors) and `timeout_seconds`. By convention the **`api_input`** and
**`tool_input`** stages run `fail_open: false` (fail-closed at the trust
boundary) while **`api_output`** runs `fail_open: true` (don't lose a
clean reply to a transient provider blip).

## Policy YAML env-var expansion

The policy loader expands `${VAR}` and `${VAR:default}` anywhere in a
YAML scalar. When the **entire** scalar is a single `${VAR}` reference,
the resolved value is JSON-parsed so lists / numbers / bools / dicts
round-trip with the correct type:

```yaml
# String substitution (always string)
api_key: ${AZURE_CONTENT_SAFETY_KEY}

# Whole-value substitution -> typed
phrases:         ${BANNED_PHRASES_JSON:[]}         # -> list[str]
severity_threshold: ${ACS_SEVERITY:2}              # -> int
blocklist_names: ${ACS_BLOCKLISTS:[]}              # -> list[str]
fail_open:       ${ACS_FAIL_OPEN:false}            # -> bool

# Partial substitution inside a larger string -> always string
endpoint: https://${ACS_HOST}.cognitiveservices.azure.com/
```

Unset variables without a default substitute as an empty string and
emit a warning at boot. Use this to drive per-environment phrase lists,
blocklist names, thresholds, or endpoints from your deployment manifest
without forking the YAML.



The service is domain-neutral. Consumers add their own rules at two
escalating levels — pick the lowest one that does the job.

### Level 1 — Configure built-in guards (no code, no rebuild)

Most "custom" requirements are just **tuning** of guards that already
ship. Edit your policy YAML and recreate the container.

```yaml
# my-policy.yaml
id: retail-helpdesk
description: Customer-support assistant, English only.

input:
  - banned-substrings:
      enabled: true
      # Static list, OR drive from env via ${BANNED_PHRASES_JSON:[]},
      # OR let callers pass context.banned_phrases per request.
      phrases: ["ignore previous instructions", "system prompt", "jailbreak"]
      case_sensitive: false
      allow_overrides: true

  - topic-relevance:
      enabled: true
      keywords: ["order","shipping","return","refund","invoice","tracking"]
      min_ratio: 0.05
      refusal_message: "I can only help with order, shipping, or returns questions."

  - competitor-mentions:
      enabled: true
      names: ["acme corp", "globex"]

  - azure-content-safety:
      enabled: true
      severity_threshold: 2
      blocklist_names: ${ACS_BLOCKLIST_NAMES:[]}

output:
  - bias-detect:
      enabled: true
      engine: lexicon

  - azure-content-safety:
      enabled: true
      severity_threshold: 2
      enable_protected_material: true   # block copyrighted text

  # RAG hallucination check (requires context.sources from caller).
  - azure-groundedness:
      enabled: true
      domain: Generic
      task: QnA
      require_sources: false

  # Block replies that drift outside the agent's declared task.
  - azure-task-adherence:
      enabled: false
      require_task_definition: false
```

Drop the file into your policies directory (mounted at
`/policies-extra` or baked into the image) and reload:

```bash
docker compose up -d --force-recreate guardrails
# or, on Kubernetes
kubectl rollout restart deploy/guardrails
```

### Level 2 — Code-based custom guard (one Python file)

When configuration is not enough — e.g. you need an LLM-as-judge, a
ML classifier, or a call to a domain microservice — write a Guard
subclass. The canonical reference implementation is
[`app/core/guards/topic_relevance.py`](app/core/guards/topic_relevance.py).

Authoring checklist:

1. Create `app/core/guards/<my_guard>.py`.
2. `class MyGuard(Guard)` with `name`, `stage`, `description`.
3. Accept config via `__init__(**config)`; store any compiled state.
4. Implement `async def check(self, text, *, context=None) -> GuardCheckResult`.
   Use `self._allow(...)`, `self._sanitize(...)`, or `self._block(...)`.
5. **Never raise** on adversarial input — return BLOCK with a reason.
6. Register at module load:
   `register_guard("<my-guard-name>", lambda cfg: MyGuard(**cfg))`.
7. Add an import line in `app/core/guards/__init__.py` (side-effect import).
8. Reference the new guard name in your policy YAML.
9. Rebuild the image (`docker compose up -d --build guardrails`).
10. Verify via the readiness probe and a smoke `/v1/check` call.

Tiny example — a SHOUTING detector that sanitizes ALL-CAPS messages:

```python
# app/core/guards/no_shouting.py
from __future__ import annotations
from typing import Any
from ..base import Guard, GuardCheckResult, GuardStage
from ..registry import register_guard

class NoShoutingGuard(Guard):
    name = "no-shouting"
    stage = GuardStage.OUTPUT
    description = "Lowercase replies that are mostly uppercase."

    def __init__(self, **cfg: Any) -> None:
        super().__init__(**cfg)
        self.threshold = float(cfg.get("threshold", 0.6))

    async def check(self, text, *, context=None):
        letters = [c for c in text if c.isalpha()]
        if not letters:
            return self._allow(text)
        ratio = sum(c.isupper() for c in letters) / len(letters)
        if ratio < self.threshold:
            return self._allow(text, score=ratio)
        return self._sanitize(
            text.lower(),
            reasons=[f"shouting ratio={ratio:.2f}"],
            categories=["style.shouting"],
            score=ratio,
        )

register_guard("no-shouting", lambda cfg: NoShoutingGuard(**cfg))
```

### Per-request overrides (callers tune guards on the fly)

When `GUARDRAILS_ALLOW_REQUEST_OVERRIDES=true`, callers can override a
**whitelisted** set of guard fields per request without changing the
policy. The allowlist is in `GUARDRAILS_OVERRIDABLE_KEYS`; security-
critical fields (`enabled`, `endpoint`, `api_key`, ...) are blocked by
`GUARDRAILS_FORBIDDEN_OVERRIDE_KEYS`. Example:

```json
POST /v1/check
{
  "policy_id": "default",
  "text": "...",
  "overrides": {
    "azure-pii-detection": { "min_confidence": 0.9 }
  }
}
```

Unknown guard names or forbidden keys → HTTP 400. Operators retain
full control over which guards run; consumers only tune thresholds.

## Roadmap

* [x] Lift service to standalone repo layout
* [x] AAD JWT validator alongside static bearer (env-driven)
* [x] `bankbuddy-agent` consumes the service over plain HTTP
* [x] Six-checkpoint pipeline (`api_input`, `input`, `tool_input`,
  `output`, `tool_output`, `api_output`) wired end-to-end through the
  BankBuddy agent and visualised in the UI Request-flow panel
* [ ] Helm chart + AKS Bicep
* [ ] `/metrics` (Prometheus) and OpenTelemetry
* [ ] Optional thin client libraries (.NET / TypeScript / Java) - HTTP is the contract
* [ ] `POST /v1/check/batch` for high-throughput consumers
