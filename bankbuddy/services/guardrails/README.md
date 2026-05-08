# BankBuddy Guardrails Service

An **internal-only** HTTP service that evaluates user input and assistant output against a configurable pipeline of safety, privacy, and policy guards. Any team in the organization can call it - they never need to know which provider (Azure AI Content Safety, Azure AI Language, custom heuristics, ...) sits behind a guard.

## 1. Why a separate service?

Before this refactor, the guard pipeline ran in-process inside the `agent` container. That worked, but every team that wanted the same guards had to vendor the code, manage Azure tokens, and ship guard upgrades themselves.

Pulling guardrails into its own container gives us:

- **Single deployment unit** for safety logic. Patch a guard once, every consumer benefits.
- **Centralized auth** to Azure AI services. AAD tokens for Content Safety and Language live only on this container.
- **Stable contract** (`POST /v1/check`). Consumers in any language can call it; they only depend on a tiny JSON shape.
- **Independent scaling and rollout**. Guard latency or throughput problems do not block agent releases.
- **Network-level isolation**. The service is reachable only from the docker-compose `internal` network - it never publishes a port.

## 2. Architecture

```
                +------------------+               +------------------------+
                |  bankbuddy-api   |               |  bankbuddy-ui (web)    |
                +---------+--------+               +-----------+------------+
                          |                                    |
                          v                                    v
        edge network --------------------------------------------------
                          |
                          v
                +------------------+
                |  bankbuddy-agent |  GUARDRAILS_MODE=remote
                +---------+--------+
                          | HTTP + Bearer token
        internal network  v
                +-------------------------+        +---------------------------+
                |  bankbuddy-guardrails   | -----> |  Azure AI Content Safety  |
                |  (this service, :8001)  |        |  Azure AI Language        |
                +-------------------------+        +---------------------------+
```

The `guardrails` service is attached **only** to the `internal` docker-compose network. It is not reachable from the host or the `edge` network.

## 3. API contract

All requests must include a bearer token:

```
Authorization: Bearer <GUARDRAILS_INTERNAL_TOKEN>
```

The token is validated with `secrets.compare_digest` to avoid timing attacks. A missing or wrong token returns `401`.

### `POST /v1/check`

Request:

```json
{
    "stage": "input",          // "input" or "output"
    "text": "How do I check my balance?",
    "policy_id": "bankbuddy-default",   // optional, falls back to GUARDRAILS_DEFAULT_POLICY_ID
    "context": {                         // optional, free-form metadata for guards
        "user_id": "u-123",
        "session_id": "s-abc"
    }
}
```

Response:

```json
{
    "decision": "allow",            // "allow" | "sanitize" | "block"
    "sanitized_text": "How do I check my balance?",
    "stage": "input",
    "policy_id": "bankbuddy-default",
    "duration_ms": 412.7,
    "block_reasons": [],
    "block_categories": [],
    "guards": [
        {
            "name": "azure-content-safety",
            "decision": "allow",
            "reasons": [],
            "categories": [],
            "score": 0.0,
            "metadata": { "severities": { "Hate": 0, "SelfHarm": 0, "Sexual": 0, "Violence": 0 } }
        }
    ],
    "request_id": "7c352584-503f-4ea8-9e59-35dbdef0a15d"
}
```

Decision semantics:

| `decision`  | Caller behaviour                                                                  |
|-------------|-----------------------------------------------------------------------------------|
| `allow`     | Use `text` (unchanged). Continue normally.                                        |
| `sanitize`  | Use `sanitized_text` instead of the original. Continue normally.                  |
| `block`     | Do **not** send `text` downstream. Show `block_reasons[]` (or your own message).  |

### `GET /v1/policies`

Lists every policy bundle the service has loaded:

```json
{
    "policies": [
        {
            "id": "bankbuddy-default",
            "description": "Default policy for the BankBuddy retail assistant.",
            "input_guards": ["azure-content-safety", "azure-pii-detection", "banking-relevance"],
            "output_guards": ["azure-content-safety", "azure-pii-detection"]
        }
    ]
}
```

### `GET /v1/policies/{id}`

Returns the same shape as a single entry above. `404` if unknown.

### `GET /healthz`

Liveness probe. Always `200 {"status":"ok"}` if the process is up.

### `GET /readyz`

Readiness probe. Returns `200` only when the default policy has loaded and built a pipeline; otherwise `503`. Used by docker-compose `depends_on: condition: service_healthy`.

## 4. Policies

A **policy** is a YAML file that lists the input guards and output guards to run, in order. Files live in `/app/services/guardrails/app/policies/` inside the container (baked into the image).

Example - `bankbuddy-default.yaml`:

```yaml
id: bankbuddy-default
description: Default policy for the BankBuddy retail assistant.

input:
    - name: azure-content-safety
        config: {}
    - name: azure-pii-detection
        config: {}
    - name: banking-relevance
        config:
            min_ratio: 0.05
            min_length: 20
            refusal_message: "I can only help with banking questions - accounts, cards, transfers, loans, ATMs."

output:
    - name: azure-content-safety
        config: {}
    - name: azure-pii-detection
        config: {}
```

Authoring rules:

1. `id` must be unique and URL-safe.
2. Each guard `name` must exist in the registry. The loader validates this at boot and refuses to start if a guard is unknown.
3. `config` is passed to the guard's constructor. Unknown keys are ignored.
4. Per-guard runtime overrides are still honoured via the existing env vars (`GUARD_<NAME>_CONFIG_OVERRIDE`, JSON). Useful for hotfixes without rebuilding the image.

To add a new policy: drop another `.yaml` file in `app/policies/` and rebuild the image.

## 5. Auth

Phase 1 - **bearer token**.

- The shared secret lives in `GUARDRAILS_INTERNAL_TOKEN`.
- It is validated on every request with `secrets.compare_digest`.
- The token is **never** logged. Requests are logged with `request_id`, `policy_id`, `decision`, and `duration_ms` only.

Phase 2 (roadmap) - **mTLS** between consumers and guardrails. The current bearer flow can coexist with mTLS as defence-in-depth.

## 6. Fail modes

The agent's `RemoteGuardrailPipeline` follows our agreed policy:

| Stage  | Fail mode    | Behaviour when guardrails is unreachable / errors                         |
|--------|--------------|---------------------------------------------------------------------------|
| input  | fail-closed  | The agent treats the request as **blocked** and returns the block message |
| output | fail-open    | The agent **allows** the response and emits a degraded-mode log entry     |

Rationale: refusing user input on outage is annoying but safe. Refusing already-generated assistant responses on outage causes user-visible failures with no security upside, so we degrade gracefully and rely on logs/alerts.

## 7. Configuration

| Env var                          | Default                                       | Purpose                                                                 |
|----------------------------------|-----------------------------------------------|-------------------------------------------------------------------------|
| `GUARDRAILS_INTERNAL_TOKEN`      | `please-rotate-this-token`                    | Bearer token consumers must present                                     |
| `GUARDRAILS_DEFAULT_POLICY_ID`   | `bankbuddy-default`                           | Used when a request omits `policy_id`                                   |
| `GUARDRAILS_POLICIES_DIR`        | `/app/services/guardrails/app/policies`       | Where to load `*.yaml` policies from                                    |
| `AZURE_CONTENT_SAFETY_ENDPOINT`  | -                                             | Azure AI Content Safety endpoint                                        |
| `AZURE_CONTENT_SAFETY_AAD_TOKEN` | -                                             | AAD token for Content Safety (refreshed by `tools/refresh-aad-token.ps1`) |
| `AZURE_LANGUAGE_ENDPOINT`        | -                                             | Azure AI Language endpoint                                              |
| `AZURE_LANGUAGE_AAD_TOKEN`       | -                                             | AAD token for Language                                                  |
| `GUARD_<NAME>_CONFIG_OVERRIDE`   | -                                             | JSON merged into a guard's config at boot (per guard)                   |

## 8. Client integration

### Python (using the shared contracts)

```python
import httpx
from bankbuddy_shared.contracts.guardrails import GuardrailsCheckRequest, GuardrailsCheckResponse

req = GuardrailsCheckRequest(stage="input", text=user_text, policy_id="bankbuddy-default")
async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.post(
                "http://guardrails:8001/v1/check",
                headers={"Authorization": f"Bearer {token}"},
                json=req.model_dump(),
        )
        r.raise_for_status()
        result = GuardrailsCheckResponse.model_validate(r.json())

if result.decision == "block":
        return refuse(result.block_reasons)
text_to_use = result.sanitized_text or user_text
```

The reference consumer is [`services/agent/app/guardrails_client.py`](../agent/app/guardrails_client.py); it duck-types the original in-process pipeline so existing agent code keeps working.

### .NET

```csharp
using var http = new HttpClient { BaseAddress = new Uri("http://guardrails:8001") };
http.DefaultRequestHeaders.Authorization = new("Bearer", token);

var body = new {
        stage = "input",
        text = userText,
        policy_id = "bankbuddy-default"
};
var resp = await http.PostAsJsonAsync("/v1/check", body);
resp.EnsureSuccessStatusCode();
var json = await resp.Content.ReadFromJsonAsync<JsonElement>();
var decision = json.GetProperty("decision").GetString();
```

### Node / TypeScript

```ts
const r = await fetch("http://guardrails:8001/v1/check", {
    method: "POST",
    headers: {
        "Authorization": `Bearer ${token}`,
        "Content-Type": "application/json",
    },
    body: JSON.stringify({ stage: "input", text: userText, policy_id: "bankbuddy-default" }),
});
const result = await r.json();
if (result.decision === "block") { /* refuse */ }
```

## 9. Running locally

```pwsh
cd bankbuddy
docker compose build guardrails
.\tools\refresh-aad-token.ps1     # acquires AAD tokens and recreates the guardrails container
docker compose up -d
```

Verify:

```pwsh
docker compose logs guardrails | Select-String "loaded policy"
docker exec bankbuddy-guardrails python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8001/readyz').read())"

# end-to-end check from another container on the internal network
docker exec bankbuddy-agent python -c "import httpx; r=httpx.post('http://guardrails:8001/v1/check', headers={'Authorization':'Bearer please-rotate-this-token'}, json={'stage':'input','text':'How do I check my balance?'}); print(r.status_code, r.json()['decision'])"
```

## 10. Switching the agent between local and remote

The agent honours `GUARDRAILS_MODE`:

- `GUARDRAILS_MODE=remote` (default) - call this service over HTTP.
- `GUARDRAILS_MODE=local` - run the in-process pipeline (legacy path, kept for one release for tests and break-glass).

Both modes use the **same guard implementations**, so behaviour is identical.

## 11. Troubleshooting

| Symptom                                        | Likely cause / fix                                                                |
|------------------------------------------------|-----------------------------------------------------------------------------------|
| Caller gets `401`                              | Token mismatch. Check `GUARDRAILS_INTERNAL_TOKEN` on both sides.                  |
| Caller gets `404 policy not found`             | `policy_id` typo or YAML file missing from image. Rebuild after editing policies. |
| `/readyz` returns `503`                        | Default policy failed to load. Check `docker compose logs guardrails` at startup. |
| Guard returns `degraded` decision              | Upstream Azure call failed. Check AAD token freshness and endpoint reachability.  |
| Agent shows banking-relevance blocking valid Q | Tune `min_ratio` / `min_length` in the policy YAML and rebuild.                   |
| `Connection refused` from agent                | Guardrails container not on the `internal` network or not yet healthy.            |

## 12. Known limitations (Phase 1)

- **Guard code is COPY'd from the agent at image build time.** The Dockerfile copies `services/agent/app/guardrails` into `app/core` so we have a single source of truth without a third package. A follow-up will move guard code into a shared library installable via `pip`.
- **Bearer token only** - no mTLS yet.
- **Policies are baked into the image.** Hot-reload from a mounted ConfigMap / volume is planned.
- **No multi-tenant policy scoping.** Today every consumer can call every loaded policy. Per-tenant policy ACLs are on the roadmap.
