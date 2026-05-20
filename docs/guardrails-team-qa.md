# Guardrails Team Q&A — Meeting Brief

> **Audience:** Engineering + Compliance review meeting
> **Scope:** Answers to the 12 team questions, anchored in the BankBuddy reference implementation in this repo and the Azure 8-layer plan in [guardrails-implementation-plan.md](guardrails-implementation-plan.md) / [guardrails-sdks-and-architecture.md](guardrails-sdks-and-architecture.md).
> **Repo layout reminder:** the OSS guardrails service lives under [guardrails-service](../guardrails-service/), the agent integration under [bankbuddy/services/agent](../bankbuddy/services/agent/), and policies are YAML under [guardrails-service/app/policies](../guardrails-service/app/policies/).

---

## 1. How does `X-Policy-Id` header injection work?

**Short answer:** APIM stamps the header from JWT claims; clients can't pick their own policy. The agent forwards it to the guardrails service / Azure OpenAI.

**Azure side (planned):**

- APIM inbound policy validates JWT, then runs a `<choose>` block on `role` / subscription tier and emits `x-policy-id: strict-production-v1` with `exists-action="override"` (any client-supplied value is discarded).
- Backend resolves the header to a deployment + `raiPolicyName` from `config/rai-mapping.yaml`, or passes it through as the `rai-policy` query parameter on AOAI calls.
- Unknown / missing policy → 400 at APIM. Allow-list is enforced.
- Full APIM XML and acceptance criteria in [guardrails-implementation-plan.md §Task 1130](guardrails-implementation-plan.md).

**This repo (today):**

- The agent's HTTP client passes a policy id to the guardrails service. See [services/agent/app/guardrails_client.py](../bankbuddy/services/agent/app/guardrails_client.py) — `policy_id="bankbuddy-default"` is sent on every request.
- The guardrails service loads the matching YAML from [guardrails-service/app/policies/](../guardrails-service/app/policies/) and builds the pipeline from it.
- Auth is `Authorization: Bearer <token>` to the guardrails service ([services/agent/app/guardrails_client.py](../bankbuddy/services/agent/app/guardrails_client.py)); in production this is replaced by APIM-issued JWT + Managed Identity.

### 1.1 APIM + RAI policy architecture (end-to-end)

The diagram below shows how a request flows through APIM, how `x-policy-id` is injected, how it maps to a named RAI policy, and where each Azure-managed safety check fires (L5/L6/L7 inside AOAI, L8 post-process). Tenant-A is a **public customer-facing app** (gets `strict-production`); Tenant-B is an **internal researcher** (gets `permissive-research`).

![APIM + RAI architecture](diagrams/apim-rai-architecture.png)

<details>
<summary>Mermaid source (click to expand)</summary>

```mermaid
flowchart TB
    classDef client fill:#fef3c7,stroke:#b45309,color:#7c2d12
    classDef apim fill:#e0f2fe,stroke:#0369a1,color:#0c4a6e
    classDef agent fill:#ede9fe,stroke:#6d28d9,color:#3b0764
    classDef rai fill:#fce7f3,stroke:#be185d,color:#831843
    classDef cs fill:#dbeafe,stroke:#1d4ed8,color:#1e3a8a
    classDef cfg fill:#f1f5f9,stroke:#475569,color:#0f172a
    classDef obs fill:#f3f4f6,stroke:#374151,color:#111827

    %% ---------- Clients ----------
    A1["Tenant-A client<br/>(public app)<br/>JWT role=customer"]:::client
    A2["Tenant-B client<br/>(researcher)<br/>JWT role=researcher"]:::client

    %% ---------- Edge ----------
    FD["Azure Front Door + WAF<br/>OWASP rules, TLS, geo-block"]:::apim

    %% ---------- APIM ----------
    subgraph APIM["Azure API Management — single ingress for all tenants"]
      direction TB
      P1["1) validate-jwt<br/>(Entra issuer + audience)"]:::apim
      P2["2) rate-limit-by-key<br/>+ quota by subscription"]:::apim
      P3["3) &lt;choose&gt; on JWT claims<br/>role=customer  → strict-production-v1<br/>role=internal → moderate-internal-v1<br/>role=researcher→ permissive-research-v1"]:::apim
      P4["4) set-header<br/>x-policy-id: &lt;resolved&gt;<br/>exists-action=override<br/>(client value is discarded)"]:::apim
      P5["5) send-request → Azure AI Language PII<br/>(inbound pre-scan / pre-redact)"]:::apim
      P6["6) Validate header against allow-list<br/>unknown id → 403"]:::apim
      P1 --> P2 --> P3 --> P4 --> P5 --> P6
    end
    class APIM apim

    %% ---------- App tier ----------
    subgraph APP["L3 — Agent service (Container Apps / App Service)"]
      direction TB
      M1["LangChain middleware<br/>reads x-policy-id<br/>selects deployment+RAI mapping"]:::agent
      M2["AzureContentModerationMiddleware<br/>analyze_text + detect_jailbreak (input)"]:::agent
      M3["Tool allow-list +<br/>function-call schema check"]:::agent
      M4["AzureContentModerationMiddleware<br/>analyze_text + detect_groundedness +<br/>detect_protected_material (output)"]:::agent
      M5["PII redaction on output<br/>recognize_pii_entities"]:::agent
      M1 --> M2 --> M3
      M4 --> M5
    end

    %% ---------- AOAI / Foundry ----------
    subgraph AOAI["Azure OpenAI / Foundry deployment"]
      direction TB
      D1["Deployment: gpt-4o-customer<br/>raiPolicyName=strict-production-v1"]:::rai
      D2["Deployment: gpt-4o-internal<br/>raiPolicyName=moderate-internal-v1"]:::rai
      D3["Deployment: gpt-4o-research<br/>raiPolicyName=permissive-research-v1"]:::rai

      subgraph RAIENG["RAI engine — runs automatically on every model call"]
        direction TB
        L5["L5 Named RAI policy<br/>severity thresholds:<br/>Hate / Sexual / Violence / Self-harm<br/>Prompt-Shield (direct + indirect)<br/>Protected-material text + code"]:::cs
        L6["L6 Microsoft default filters<br/>(always-on baseline)"]:::cs
        L7["L7 Custom blocklists<br/>fin-strict / fin-internal / fin-research<br/>(SSN, account#, regulated phrases,<br/>competitor names, codenames)"]:::cs
        L5 --> L6 --> L7
      end

      D1 --> RAIENG
      D2 --> RAIENG
      D3 --> RAIENG
    end

    %% ---------- Policy / config plane ----------
    subgraph CFG["Control plane — policy-as-code"]
      direction TB
      C1["Bicep: rai-policies.bicep<br/>Microsoft.CognitiveServices/<br/>accounts/raiPolicies@2024-10-01"]:::cfg
      C2["config/rai-mapping.yaml<br/>deployment → policy"]:::cfg
      C3["config/blocklists/*.csv<br/>PR-reviewed by Compliance"]:::cfg
      C4["Azure Policy<br/>'deployment must reference<br/>approved RAI policy'"]:::cfg
      C5["BlocklistClient sync job<br/>(Azure Function, scheduled)"]:::cfg
      C1 -. deploys .-> RAIENG
      C2 -. assigns .-> D1
      C2 -. assigns .-> D2
      C2 -. assigns .-> D3
      C3 --> C5
      C5 -. updates .-> L7
      C4 -. enforces .-> RAIENG
    end

    %% ---------- Observability ----------
    subgraph OBS["Cross-cutting — telemetry + audit"]
      direction TB
      O1["Log Analytics workspace<br/>law-aiagents-&lt;env&gt;<br/>tables: AzureDiagnostics,<br/>AOAIRequestResponse,<br/>RAIContentFilterEvents,<br/>AppGuardrailEvents"]:::obs
      O2["App Insights<br/>OTel spans tagged<br/>x-policy-id, request-id, tenant-id"]:::obs
      O3["ADLS Gen2 archive<br/>7-year retention"]:::obs
      O4["Defender for Cloud<br/>AI workload alerts"]:::obs
      O1 --> O3
    end

    %% ---------- Flow ----------
    A1 --> FD
    A2 --> FD
    FD --> P1
    P6 --> M1
    M3 -- "POST /chat/completions<br/>?rai-policy=&lt;x-policy-id&gt;" --> D1
    M3 -. "(researcher path)" .-> D3
    RAIENG -- "filtered response<br/>+ filter results" --> M4
    M5 --> P6
    P6 -- "200 OK + scrubbed body" --> A1
    P6 -. .-> A2

    %% Telemetry edges
    APIM -.-> O1
    APP -.-> O2
    AOAI -.-> O1
    O2 --> O1
    AOAI -.-> O4
```

</details>

**How to read the diagram:**

| Step | What happens | Owner |
|---|---|---|
| FD → APIM | TLS, WAF, geo-block — coarse network filter | Platform |
| APIM 1–2 | Identity, rate limit — every request authenticated | Platform |
| APIM 3–4 | **`x-policy-id` is set from JWT claims**, client value overridden | Platform |
| APIM 5 | Inbound PII pre-scan via Language API — strips PII before app sees it | Compliance |
| APIM 6 | Allow-list check — unknown policy id → 403 | Platform |
| APP M1 | Agent reads header, picks deployment + mapping | App team |
| APP M2 | Input-side Content Safety + Prompt Shields | App team (uses managed) |
| APP M3 | Tool allow-list — refuses unsafe function calls | App team |
| AOAI L5/L6/L7 | **RAI policy fires automatically inside AOAI** — no app code | Microsoft + Risk |
| APP M4/M5 | Output-side Content Safety, Groundedness, PII redaction | App team |
| OBS | Every step emits structured logs correlated by `request-id` + `x-policy-id` | Platform |

**Key invariants this diagram enforces:**

1. **Single ingress** — every request passes APIM. No backdoor to the agent.
2. **Policy is a header, not a body field** — survives streaming, logged for free, can't be tampered with by the client.
3. **One deployment per risk tier** — `gpt-4o-customer` / `gpt-4o-internal` / `gpt-4o-research` each carry exactly one named RAI policy. Reassigning is a control-plane operation (Bicep + `az rest PATCH`), not a runtime decision.
4. **L5/L6/L7 run inside AOAI** — the agent cannot bypass them even if compromised. App-tier middleware (M2/M4) is **additional** defense, not the primary control.
5. **Compliance owns blocklists** via PR + scheduled sync; engineering can't edit them ad-hoc.

![APIM + RAI sequence](diagrams/apim-rai-sequence.png)

<details>
<summary>Mermaid source (click to expand)</summary>

### 1.2 Sequence — single request through APIM + RAI


**Block-path variant** — if L5 fires inside AOAI:

- AOAI returns `finish_reason=content_filter` with `content_filter_results` listing the category (e.g. `hate: high`).
- Agent **does not retry** — it returns `GUARDRAILS_BLOCK_MESSAGE` to APIM with the diagnostic detail in `metadata.guardrails`.
- APIM scrubs the diagnostic from the user response, logs the full detail to Log Analytics, and returns 200 with the user-safe block message (or 451 if compliance demands an explicit status code).

---

## 2. What policies are available in Azure RAI?

Two distinct things are called "policies" — keep them separate.

### (a) Built-in content filter capabilities (each Azure OpenAI / Foundry deployment)

| Capability | Severities / Modes |
|---|---|
| Hate | Safe / Low / Medium / High |
| Sexual | Safe / Low / Medium / High |
| Violence | Safe / Low / Medium / High |
| Self-harm | Safe / Low / Medium / High |
| Jailbreak — direct (Prompt Shields) | On / Off |
| Jailbreak — indirect (RAG / tool output) | On / Off |
| Protected material — text | Block / Annotate |
| Protected material — code | Block / Annotate |
| Groundedness detection (preview) | Block / Annotate |
| Custom blocklists | Bound to policy |

### (b) Named RAI policy bundles we ship (`Microsoft.CognitiveServices/accounts/raiPolicies@2024-10-01`)

| Policy | Use case | H / S / V / SH | Jailbreak | Protected material | Blocklist |
|---|---|---|---|---|---|
| `strict-production` | Customer-facing advisory, statements, KYC | Low | Block | Block | `fin-strict` |
| `moderate-internal` | Internal employee copilot | Medium | Block | Annotate | `fin-internal` |
| `permissive-research` | Sandbox / red-team | High | Annotate | Annotate | `fin-research` |

Bicep module + deployment pipeline: [guardrails-implementation-plan.md §Task 1127](guardrails-implementation-plan.md).

### (c) OSS analog in this repo

Each YAML file under [guardrails-service/app/policies/](../guardrails-service/app/policies/) is a "policy bundle." The default one [bankbuddy-default.yaml](../guardrails-service/app/policies/bankbuddy-default.yaml) wires Azure Content Safety + Azure Language PII + custom guards into input and output pipelines.

---

## 3. How to update guardrail stacks and manage versions

Everything is **policy-as-code**. No portal clicks.

### Azure RAI policies

- Bicep module `modules/ai/rai-policies.bicep` deployed by `infra/pipelines/rai-policies.yml` with manual approval gate to prod.
- Versioned naming: `strict-production-v1`, `-v2` (treat as immutable; create v2, swap the assignment).
- **Reassignment without redeploy** — `az rest PATCH .../deployments/<dep>?api-version=...` body `{ "properties": { "raiPolicyName": "<new>" } }`. Requires CR ticket. Runbook: `runbooks/change-rai-policy.md`.
- **Drift detection** (Task 1126) — CI fails the build if any deployment lacks the baseline filter or thresholds drifted from `baselines/content-filter-baseline.json`.
- Azure Policy enforces "deployment must reference an approved RAI policy."

### Custom blocklists

- CSVs in `config/blocklists/*.csv`, PR-reviewed by Compliance.
- An Azure Function reconciles repo → Content Safety Blocklist API on schedule.

### OSS pipeline (this repo)

- Policy YAML under [guardrails-service/app/policies/](../guardrails-service/app/policies/) — versioned with the service code.
- Each guard has `enabled: true|false` plus per-guard config inline. Loader in [policies/loader.py](../guardrails-service/app/policies/loader.py#L45-L70) drops disabled guards before the pipeline is built.
- Reload protocol: `docker compose up -d --force-recreate guardrails` (header comment in [bankbuddy-default.yaml](../guardrails-service/app/policies/bankbuddy-default.yaml#L1-L17)).
- New guard registration: drop a file in [guardrails-service/app/core/guards/](../guardrails-service/app/core/guards/), add it to [guards/__init__.py](../guardrails-service/app/core/guards/__init__.py), reference it by hyphenated name in the policy YAML. Authoring guide: [bankbuddy/docs/guardrails.md §5](../bankbuddy/docs/guardrails.md).

---

## 4. Integration model — inline or guardrails-as-a-service?

**Both, by design.** Defense-in-depth needs both.

| Mode | Where | What it catches | Trade-offs |
|---|---|---|---|
| **In-process inline** | L3 middleware inside the agent (LangChain `AzureContentModerationMiddleware` / local guard pipeline) | Business-logic guards: tool allow-list, banking-relevance, citation enforcement, regex PII | Fast (µs for regex), full context, can `SANITIZE` not just block |
| **Out-of-process service** | L2 APIM, L5 deployment-level RAI inside AOAI, L7 Content Safety REST, **and our own guardrails microservice** | Compliance-owned safety: RAI, Prompt Shields, blocklists, PII | Centrally owned, can't be bypassed by app code, swappable without redeploying agents |

**This repo proves the GaaS pattern.** The agent talks to a separate guardrails container over HTTP:

- Agent side: [RemoteGuardrailPipeline](../bankbuddy/services/agent/app/guardrails_client.py) — same `check_input` / `check_output` surface as the local pipeline so providers stay unchanged.
- Service side: [services/guardrails](../guardrails-service/) hosts the guards, policies, and Azure SDK calls.
- A flip of one env var in the agent switches between in-process and remote — same contract.

Rule of thumb: **business-logic guards inline, compliance/safety guards as-a-service.**

---

## 5. Which of the 8 layers detect sensitive data leaks?

PII / PHI / secrets get three chances:

| Layer | Detector | Purpose |
|---|---|---|
| L2 APIM | `<send-request>` to Azure AI Language PII | Inbound pre-redact before app sees it |
| L3 input | `azure-ai-textanalytics` `recognize_pii_entities(domain="phi")` + Presidio fallback | Strip PII / PHI before model call |
| L3 (OSS) | [pii_detect.py](../guardrails-service/app/core/guards/pii_detect.py) (regex) + [azure_pii_detection.py](../guardrails-service/app/core/guards/azure_pii_detection.py) | Same, in the OSS pipeline |
| L7 | Content Safety blocklist regexes for SSN / account # / IBAN | Catches templated leaks the LLM might emit |
| L8 output | `recognize_pii_entities` on response + [output_pii_redact.py](../guardrails-service/app/core/guards/output_pii_redact.py) + [secret_leak.py](../guardrails-service/app/core/guards/secret_leak.py) | Last line — masks SSN/card/IBAN, blocks AWS keys, GitHub PATs, JWTs, OpenAI keys, private keys, bearer tokens |

**Default policy already wires both stages** — [bankbuddy-default.yaml](../guardrails-service/app/policies/bankbuddy-default.yaml#L40-L96):

```yaml
input:
  - pii-detect:           { enabled: true, mode: block }
  - azure-pii-detection:  { enabled: true, mode: block, min_confidence: 0.3 }
output:
  - azure-pii-detection:  { enabled: true, mode: sanitize, min_confidence: 0.3 }
  - pii-detect:           { enabled: true, mode: sanitize }
  - secret-leak:          { enabled: true }
```

L8 is the layer that "detects leaks" specifically (post-model). L2 / L3 prevent the data from being sent to the model in the first place.

---

## 6. Which layers are rule-based vs LLM/SLM-based?

| Layer / guard | Engine type |
|---|---|
| L1 Network / WAF | Rule (OWASP managed rule set) |
| L2 APIM | Rule (policy XML, regex) |
| L3 — `token-limit`, `banned-substrings`, `secret-leak`, `output-pii-redact`, `competitor-mentions`, `banking-relevance` | Rule / regex / keyword |
| L3 — `pii-detect` regex engine | Rule (regex) |
| L3 — `pii-detect` Presidio engine, `azure-pii-detection` | **SLM / NER model** |
| L3 — `prompt-injection` heuristic | Rule (heuristic scoring) |
| L3 — `azure-content-safety` (Prompt Shields, H/S/V/SH) | **SLM** (Microsoft-trained classifier) |
| L3 — `toxicity` keyword engine | Rule |
| L3 — `toxicity` detoxify engine | **SLM** |
| L4 Agent RAI | Mixed — config that gates the SLMs in L5/L6 |
| L5 Deployment RAI / L6 default filters | **SLM** |
| L7 Custom blocklists | Rule |
| L8 — `analyze_text`, `detect_jailbreak`, `detect_text_protected_material` | **SLM** |
| L8 — `detect_groundedness` | **LLM-judge** (preview) |
| Eval — `azure-ai-evaluation` | LLM-judge for groundedness/coherence; SLM for content safety |

**Concrete examples in this repo:**

- Pure rule guard: [token_limit.py](../guardrails-service/app/core/guards/token_limit.py), [secret_leak.py](../guardrails-service/app/core/guards/secret_leak.py).
- Heuristic guard: [prompt_injection.py](../guardrails-service/app/core/guards/prompt_injection.py).
- SLM-backed guard: [azure_content_safety.py](../guardrails-service/app/core/guards/azure_content_safety.py), [azure_pii_detection.py](../guardrails-service/app/core/guards/azure_pii_detection.py).

Headline: regex/rules where they suffice, SLMs from Azure Content Safety / Language at L3 / L5 / L8, LLM-judges only for groundedness and offline eval (cost reasons).

---

## 7. How are logs captured?

Single sink, structured, correlated by `request-id` + `x-policy-id`.

### Azure side

- **Diagnostic Settings** on Front Door, APIM, Foundry, AOAI, Container Apps → Log Analytics workspace `law-aiagents-<env>`.
- Tables and retention (Task 1125):
  - 90 d operational: `AzureDiagnostics`, `AOAIRequestResponse`.
  - 365 d audit: `RAIContentFilterEvents`, `AuditLogs`, `AppGuardrailEvents`.
- **App-side OTel** — `azure-monitor-opentelemetry` `configure_azure_monitor()`, spans tagged `x-policy-id`, `request-id`, `tenant-id`, per-guard `decision/score/reasons/categories/duration_ms`.
- **Continuous Export** Log Analytics → ADLS Gen2 (`storage-aiagents-archive`) for 7-year retention.
- **Defender for Cloud — AI workload** raises alerts on jailbreak / injection surges.
- **Purview** catalogs prompts and responses for lineage.

### OSS side

Every guard returns a `GuardCheckResult` with `guard_name / decision / reasons / categories / score / metadata` — see [services/agent/app/guardrails_client.py](../bankbuddy/services/agent/app/guardrails_client.py). The full `PipelineResult` (with `duration_ms` and the list of checks) is attached to `AgentInvokeResponse.metadata.guardrails` and shipped to App Insights — never returned to the user. The user sees only `GUARDRAILS_BLOCK_MESSAGE`.

---

## 8. Input-only / output-only / both — how does GaaS pick the right one?

The agent **does not pick.** The orchestration picks based on lifecycle position.

**This repo, concretely:**

- Each guard declares its stage as a class attribute (`stage = GuardStage.INPUT | OUTPUT | TOOL_OUTPUT | BOTH`) — see [services/agent/app/guardrails_client.py](../bankbuddy/services/agent/app/guardrails_client.py).
- The policy YAML has explicit `input:` / `output:` / `tool_output:` sections — [bankbuddy-default.yaml](../guardrails-service/app/policies/bankbuddy-default.yaml#L23-L96). The loader builds three independent pipelines.
- The agent calls `pipeline.check_input(text)` **before** the LLM, `pipeline.check_tool_output(json)` **before each tool result is fed back**, and `pipeline.check_output(reply)` **after** the LLM. There's no "agent picks a guard" decision point.
- Tool-output stage skips the HTTP round-trip entirely if no tool-output guards are configured — see [services/agent/app/guardrails_client.py](../bankbuddy/services/agent/app/guardrails_client.py).
- Per-tenant variations are handled by selecting a different policy YAML (different `policy_id`) — same agent code, different guard set.

For Azure GaaS the same principle applies: APIM/middleware orchestrates, calling `analyze_text(category=Prompt)` on input and `analyze_text(category=Completion) + detect_groundedness + detect_protected_material` on output. The SDK call site, not the agent, decides stage.

**Bottom line:** stage selection is a property of the guard + the lifecycle hook, never a runtime agent decision.

---

## 9. Hallucination detection

Four layers, ordered cheapest-first:

1. **Citation enforcement (rule, L8)** — for any RAG flow, reject responses that contain a `$` amount or `%` without an inline citation to a retrieved chunk. Deterministic, fast.
2. **Azure AI Content Safety — Groundedness Detection (preview, L8)** — `ContentSafetyClient.detect_groundedness()` with the response and the grounding sources; returns ungrounded segments with offsets. Block or annotate per policy.
3. **Optional inline LLM-judge guard** for high-stakes flows (advisory). Add a custom guard that calls a small judge model with `(response, sources)` and blocks below a threshold. Adds one LLM round-trip — only enable on the `strict-production` policy.
4. **Offline / CI** — `azure-ai-evaluation.GroundednessEvaluator` runs nightly against the 64-case adversarial harness; threshold gates promotion.

**Implementation hook in this repo:** add a `groundedness` guard under [guardrails-service/app/core/guards/](../guardrails-service/app/core/guards/) following the [azure_content_safety.py](../guardrails-service/app/core/guards/azure_content_safety.py) pattern, register in `guards/__init__.py`, reference from the OUTPUT section of the policy YAML. The grounding sources travel through the per-call `context` dict already plumbed in `check_output(text, context=...)`.

---

## 10. Fail fast and retry design

### Fail fast — block decisions are terminal

- A guard returning `BLOCK` short-circuits the pipeline; no further guards run, no LLM call. See `PipelineResult.allowed` flag — [services/agent/app/guardrails_client.py](../bankbuddy/services/agent/app/guardrails_client.py).
- APIM rejects on missing JWT, unknown `x-policy-id`, rate-limit breach before the agent sees the request.
- Per-guard timeouts (5 s default in this repo — [services/agent/app/guardrails_client.py](../bankbuddy/services/agent/app/guardrails_client.py)). On timeout/5xx/unreachable:

| Stage | Failure mode | Why |
|---|---|---|
| INPUT | **Fail-closed (BLOCK)** | Never let a degraded guardrails service silently expose the LLM to user input |
| OUTPUT | **Fail-open (ALLOW + log)** | Never let a degraded service hold a clean answer hostage |
| TOOL_OUTPUT | **Fail-open** | Same as OUTPUT; operators disable the affected tool at agent level if they need fail-closed |

This split is documented at the top of [services/agent/app/guardrails_client.py](../bankbuddy/services/agent/app/guardrails_client.py) and is locked-in policy.

- Guards must **never raise** — base class contract. A throwing guard is treated as `ALLOW` and logged.

### Retry

- **Idempotent SDK calls only** (`analyze_text`, `recognize_pii_entities`, `detect_jailbreak`) — Azure SDK exponential backoff, max 2 retries, 200 ms cap. We just configure `retry_total=2`.
- **Never retry the LLM on `content_filter` finish reason** — that's a guardrail decision, not a transient error. Return blocked response.
- LLM 429 / 5xx — backoff + fallback deployment (PTU + standard).
- User-facing block is final; UI surfaces the block message and a "rephrase" CTA. No client-side auto-retry.

---

## 11. Test and evaluate guardrails before shipping

Layered, executable today on this repo:

| Layer | Mechanism | Where |
|---|---|---|
| Unit tests | One block + one allow per guard | [bankbuddy/tests/test_guardrails.py](../bankbuddy/tests/test_guardrails.py) |
| Pipeline integration | Full input + output pipeline on canned messages | same file |
| QA on running service | `/internal/guardrails/list` and `/internal/guardrails/check` exercise a single guard or a full pipeline on arbitrary text **without invoking the LLM** — used for threshold tuning | [bankbuddy/docs/guardrails.md §4](../bankbuddy/docs/guardrails.md) |
| Smoke test | End-to-end check of the running stack | [bankbuddy/tests/smoke_guardrails.py](../bankbuddy/tests/smoke_guardrails.py) |
| Adversarial harness | 64 cases (jailbreaks, PII, regulated speech, hallucination, benign control) — baseline 89.8 %; promotion gate ≥ baseline | `guardrails-test` repo |
| Foundry evaluators in CI | `ContentSafetyEvaluator`, `IndirectAttackEvaluator`, `GroundednessEvaluator`, `ProtectedMaterialEvaluator` on every PR; pipeline fails on regression | [guardrails-implementation-plan.md §Task 1133](guardrails-implementation-plan.md) |
| Nightly red-team | Same evaluators on a rotating adversarial set; alerts on App Insights | same |
| Shadow mode | Deploy new guards in `mode: annotate` for 1–2 weeks, review false-positive rate with Compliance, then flip to `block` | YAML policy switch |
| Drift detection | CI verifies content-filter baseline JSON matches deployed config | Task 1126 |
| Load tests | Confirm pipeline adds < 200 ms p95 with all SDK guards on | release checklist |

---

## 12. MS AI Foundry guardrails — production availability (May 2026)

| Capability | Status |
|---|---|
| Azure AI Content Safety — H / S / V / SH | **GA** |
| Prompt Shields — direct attack | **GA** |
| Prompt Shields — indirect attack | **GA** |
| Custom blocklists | **GA** |
| Protected material — text | **GA** |
| Protected material — code | **GA** |
| Azure AI Language PII (incl. `domain="phi"`) | **GA** |
| Foundry RAI policies (named, Bicep) | **GA** |
| Agent-level RAI settings (`azure-ai-projects`) | **GA** |
| Groundedness detection | **Preview** — usable under preview SLA, pair with citation enforcement |
| `azure-ai-evaluation` content-safety / indirect-attack / protected-material evaluators | **GA** |
| `azure-ai-evaluation` quality evaluators (some) | **Preview** |
| Defender for Cloud — AI workload protection | **GA** |

**Recommendation for the meeting:**

- Ship on the GA components for the production guarantee.
- Run Groundedness in **annotate** mode at L8 plus rule-based citation enforcement until GA.
- Treat preview evaluators as CI signal only, not deployment gates, until GA.
- Always recheck the Azure updates page before each release — preview features can change API shape.

---

## Appendix A — Repo file map for reviewers

| Topic | File |
|---|---|
| Azure 8-layer plan | [docs/guardrails-implementation-plan.md](guardrails-implementation-plan.md) |
| SDK / package matrix + diagrams | [docs/guardrails-sdks-and-architecture.md](guardrails-sdks-and-architecture.md) |
| OSS guardrails authoring guide | [bankbuddy/docs/guardrails.md](../bankbuddy/docs/guardrails.md) |
| Default policy YAML | [guardrails-service/app/policies/bankbuddy-default.yaml](../guardrails-service/app/policies/bankbuddy-default.yaml) |
| Policy loader | [guardrails-service/app/policies/loader.py](../guardrails-service/app/policies/loader.py) |
| Built-in guards | [guardrails-service/app/core/guards/](../guardrails-service/app/core/guards/) |
| Agent → guardrails client (fail modes documented) | [bankbuddy/services/agent/app/guardrails_client.py](../bankbuddy/services/agent/app/guardrails_client.py) |
| Tests | [bankbuddy/tests/](../bankbuddy/tests/) |
