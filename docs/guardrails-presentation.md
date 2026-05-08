# Agent Guardrail Integration - Presentation Reference

> **User Story:** US-2 (ID 1109) - Agent Guardrail Integration
> **Domain:** Financial Services AI Agents
> **Model under test:** GPT-5.2 on Microsoft Foundry
> **Stack:** Azure AI Foundry + Azure OpenAI + Content Safety + APIM + LangChain (Python)
> **Companion docs:** [guardrails-implementation-plan.md](guardrails-implementation-plan.md), [guardrails-workitems-detailed.md](guardrails-workitems-detailed.md)

---

## How to read this document

Each task is tagged with **where the control is enforced**:

| Tag                      | Meaning                                                                                                  |
|--------------------------|----------------------------------------------------------------------------------------------------------|
| **[Code Level]**         | Implemented in the application code (Python / LangChain / SDK calls). Owned by the app team.            |
| **[Foundry Level]**      | Configured on the Azure AI Foundry / AOAI resource (RAI policies, agent versions, content filters).      |
| **[Infrastructure Level]** | Provisioned via Bicep / Azure Policy / APIM / Log Analytics. Owned by the platform team.               |

**Main focus of this deck: Code Level.** Infrastructure and Foundry items are summarized for completeness.

---

## Layer-to-Task quick map

| Layer | Description                          | Enforcement point        | Task IDs            |
|-------|--------------------------------------|--------------------------|---------------------|
| L1    | Network and identity                 | Infrastructure Level     | 1136 (proposed)     |
| L2    | API Management gateway               | Infrastructure Level     | 1130, 1136          |
| L3    | Application middleware (LangChain)   | **Code Level**           | **1132, 1129**      |
| L4    | Agent-level RAI                      | Foundry Level (via code) | **1129**            |
| L5    | Model deployment RAI policy          | Foundry Level            | 1127, 1128          |
| L6    | Default content filters              | Foundry Level            | 1126                |
| L7    | Custom blocklists                    | Foundry Level (data)     | 1131                |
| L8    | Output post-processing + evaluation  | **Code Level**           | **1135**            |
| X     | Policy-as-code, observability        | Infrastructure Level     | 1125, 1133, 1134    |

---

## Task 1125 - Define guardrail taxonomy and Log Analytics backbone

* **User Story:** US-2 (1109)
* **Task No:** 1125
* **Layer Tag:** [Infrastructure Level]
* **Topic:** Guardrail taxonomy + observability foundation
* **What is the use:** Establishes the canonical 8-layer model and the logging pipeline every other guardrail writes to. Without it, no decision is auditable.
* **How to implement:**
  * Publish `docs/guardrails-taxonomy.md` with layer ownership and bypass rules.
  * Provision `law-aiagents-<env>` Log Analytics workspace via Bicep (`modules/observability/loganalytics.bicep`).
  * Set retention: 90 days operational, 365 days audit.
  * Continuous Export to ADLS Gen2 for 7-year financial retention.
  * Enable Diagnostic Settings on APIM, Foundry, AOAI, Container Apps.

---

## Task 1126 - Default content filters on model deployments

* **User Story:** US-2 (1109)
* **Task No:** 1126
* **Layer Tag:** [Foundry Level] (audit script is [Code Level])
* **Topic:** Microsoft baseline content filter (Hate / Sexual / Violence / Self-Harm / Jailbreak)
* **What is the use:** Non-negotiable safety floor on every deployment. Stops the obvious harmful outputs before any custom logic runs.
* **How to implement:**
  * Audit every deployment with `az cognitiveservices account deployment list`.
  * Confirm Medium-block on prompt + completion for all four categories; Prompt Shields direct-attack ON.
  * Snapshot to `baselines/content-filter-baseline.json`.
  * Add a CI drift job (`scripts/check_content_filter_drift.py`) that fails on any deviation.

---

## Task 1127 - Three named RAI policies (threat profiles)

* **User Story:** US-2 (1109)
* **Task No:** 1127
* **Layer Tag:** [Foundry Level] (defined as [Infrastructure Level] Bicep)
* **Topic:** `strict-production`, `moderate-internal`, `permissive-research`
* **What is the use:** Different agents face different risk; one size does not fit all. Named policies let us apply the right severity per workload.
* **How to implement:**
  * Author `modules/ai/rai-policies.bicep` using `Microsoft.CognitiveServices/accounts/raiPolicies@2024-10-01`.
  * Each policy declares severity per category, prompt + completion sources, blocklist references.
  * Versioned (`-v1`, `-v2`); never edit in place.
  * Deploy via pipeline `infra/pipelines/rai-policies.yml` with prod approval gate.

---

## Task 1128 - Assign RAI policies to deployments

* **User Story:** US-2 (1109)
* **Task No:** 1128
* **Layer Tag:** [Foundry Level] (config in [Infrastructure Level] repo)
* **Topic:** Deployment-to-policy mapping + no-redeploy reassignment
* **What is the use:** Connects the right policy to the right model deployment, and lets us swap policies without restarting agents.
* **How to implement:**
  * Maintain `config/rai-mapping.yaml` as single source of truth.
  * Set `raiPolicyName` via Bicep parameter on `Microsoft.CognitiveServices/accounts/deployments`.
  * Reassignment: `az rest PATCH .../deployments/<name>?api-version=... -b '{"properties":{"raiPolicyName":"<new>"}}'` gated by a change ticket.

---

## Task 1129 - Agent-level RAI settings (Code Level - PRIMARY)

* **User Story:** US-2 (1109)
* **Task No:** 1129
* **Layer Tag:** [Code Level] (writes to Foundry via SDK)
* **Topic:** Per-agent-version safety profile that can only **tighten** the deployment policy
* **What is the use:** Some agents (public FAQ, KYC) need stricter rules than the shared deployment policy. This task lets the app team enforce that in code, versioned with the agent.
* **How to implement:**
  * Helper module `app/guardrails/agent_rai.py`:

    ```python
    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    def apply_agent_rai(project_endpoint: str, agent_id: str, version: str, rai_cfg: dict) -> None:
        client = AIProjectClient(project_endpoint, DefaultAzureCredential())
        client.agents.update_version(
            agent_id=agent_id,
            version=version,
            rai_settings={
                "content_filters": rai_cfg["content_filters"],
                "blocklists": rai_cfg.get("blocklists", []),
                "prompt_shields": rai_cfg.get("prompt_shields", "enabled"),
            },
        )
    ```

  * Store per-agent RAI in `agents/<name>/v<n>/rai.json`. Example for a public FAQ agent:

    ```json
    {
      "content_filters": {
        "Hate":     {"severity": "Low",  "block": true},
        "Sexual":   {"severity": "Low",  "block": true},
        "Violence": {"severity": "Low",  "block": true},
        "SelfHarm": {"severity": "Low",  "block": true}
      },
      "blocklists": ["fin-strict"],
      "prompt_shields": "enabled"
    }
    ```

  * Precedence rule: `effective.severity = max(deployment_policy.severity, agent_policy.severity)`.
  * Pipeline applies agent RAI on every agent version build.

---

## Task 1130 - Per-request override via `x-policy-id`

* **User Story:** US-2 (1109)
* **Task No:** 1130
* **Layer Tag:** [Infrastructure Level] (APIM policy) + [Code Level] (backend honors header)
* **Topic:** Header-driven RAI policy selection per request
* **What is the use:** The same agent can serve public, internal, and research callers using different policies, without redeploying.
* **How to implement:**
  * APIM inbound policy strips any client-supplied `x-policy-id` and re-injects based on subscription key or JWT claim:

    ```xml
    <choose>
      <when condition="@(context.Subscription.Key == 'public-tier')">
        <set-header name="x-policy-id" exists-action="override">
          <value>strict-production-v1</value>
        </set-header>
      </when>
      <when condition="@(((Jwt)context.Variables['jwt']).Claims.GetValueOrDefault('role') == 'researcher')">
        <set-header name="x-policy-id" exists-action="override">
          <value>permissive-research-v1</value>
        </set-header>
      </when>
      <otherwise>
        <set-header name="x-policy-id" exists-action="override">
          <value>moderate-internal-v1</value>
        </set-header>
      </otherwise>
    </choose>
    ```

  * Backend reads the header and forwards to AOAI / Foundry. Reject unknown values with `403`.

---

## Task 1131 - Custom finance blocklists

* **User Story:** US-2 (1109)
* **Task No:** 1131
* **Layer Tag:** [Foundry Level] (data in [Infrastructure Level] repo, sync via [Code Level] Function)
* **Topic:** `fin-strict`, `fin-internal`, `fin-research` blocklists
* **What is the use:** Catches finance-specific risk that generic content filters miss: regulated phrases ("guaranteed return"), competitor names, internal codenames, account/SSN regex, OFAC keywords.
* **How to implement:**
  * Source CSVs in `config/blocklists/` with PR approval from Compliance.
  * Azure Function `functions/blocklist-sync/` reconciles repo -> Content Safety REST API on PR-merge + nightly.
  * Reference blocklists from RAI policies (Task 1127).
  * Daily sentinel test proves the sync path is healthy.

---

## Task 1132 - LangChain middleware guardrails (Code Level - PRIMARY)

* **User Story:** US-2 (1109)
* **Task No:** 1132
* **Layer Tag:** [Code Level]
* **Topic:** `AzureContentModerationMiddleware` on input AND output of every LangChain agent
* **What is the use:** Catches what the model does not, before tools are invoked and before the response leaves the process. Enforces input + output moderation, configurable per environment.
* **How to implement:**
  * `app/guardrails/middleware.py`:

    ```python
    import os
    from langchain_azure_ai.callbacks import AzureContentModerationMiddleware
    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    def build_moderation(project_endpoint: str) -> AzureContentModerationMiddleware:
        project = AIProjectClient(project_endpoint, DefaultAzureCredential())
        return AzureContentModerationMiddleware(
            project_client=project,
            categories=["Hate", "Sexual", "Violence", "SelfHarm"],
            thresholds={"Hate": 2, "Sexual": 2, "Violence": 2, "SelfHarm": 2},
            blocklists=["fin-strict"],
            prompt_shields=True,
            exit_behavior=os.getenv("GUARDRAIL_EXIT_BEHAVIOR", "raise"),
        )
    ```

  * Wire into every agent factory:

    ```python
    moderation = build_moderation(PROJECT_ENDPOINT)
    agent = build_agent(model="gpt-5.2", middleware=[moderation], tools=ALLOWED_TOOLS)
    ```

  * `exit_behavior=raise` for production; `replace` for internal copilots returns a safe canned reply.
  * Emit a structured event into `AppGuardrailEvents` per moderation decision.

---

## Task 1133 - Enforce guardrails via Azure Policy

* **User Story:** US-2 (1109)
* **Task No:** 1133
* **Layer Tag:** [Infrastructure Level]
* **Topic:** Initiative `init-aiagent-guardrails-v1` (deny non-compliant deployments)
* **What is the use:** Makes it impossible to ship a deployment without an approved RAI policy, default filter, diag settings, Private Link, or data-classification tag.
* **How to implement:**
  * Author custom policies in `infra/policy/` (Deny / DeployIfNotExists / Audit).
  * Bundle into initiative; assign at management-group scope.
  * Documented exemption flow (time-bound, Risk approval, max 30 days).

---

## Task 1134 - Update + rollback runbook

* **User Story:** US-2 (1109)
* **Task No:** 1134
* **Layer Tag:** [Infrastructure Level] (process) + [Code Level] (`scripts/rollback-rai.ps1`)
* **Topic:** Versioned policy lifecycle, dev -> test -> prod with eval gate, one-command rollback
* **What is the use:** Guardrail changes are safe, auditable, reversible. Rollback in <= 10 minutes without agent redeploy.
* **How to implement:**
  * Convention: never edit a policy in place; new behavior = new version.
  * Promotion pipeline `pipelines/rai-promote.yml` gated by the eval suite (Task 1135).
  * `scripts/rollback-rai.ps1` PATCHes `raiPolicyName` to the previous version.
  * Quarterly tabletop exercise.

---

## Task 1135 - Evaluation framework + CI gate (Code Level - PRIMARY)

* **User Story:** US-2 (1109)
* **Task No:** 1135
* **Layer Tag:** [Code Level]
* **Topic:** `guardrails-test` harness in CI + Foundry built-in evaluators
* **What is the use:** Continuously proves every layer enforces its responsibility. Blocks releases on regression.
* **How to implement:**
  * Pipeline `pipelines/guardrail-eval.yml`: runs `python run_tests.py` per PR, nightly, and pre-prod.
  * Quality gate `scripts/quality_gate.py`: pass-rate >= 98%, 0 critical, <= 1 high, no new false positives.
  * Add Foundry built-in evaluators: `hate_unfairness`, `violence`, `sexual`, `self_harm`, `direct_attack`, `indirect_attack`, `protected_material`, `groundedness`, `pii`.
  * Log Analytics workbook: block-rate per category, false-positive rate, latency P95, regression chart.
  * Quarterly red-team report.

---

## Implementing a custom guardrail for a specific agent

When a single agent (for example, a customer-facing **Investment Advisor** agent) needs rules beyond the shared stack - say, blocking any phrasing that resembles a binding investment recommendation - implement it at **[Code Level]** with a small, targeted layer added to that agent only.

### Decision tree - where should the custom rule live?

| Need                                                    | Place it here                                              |
|---------------------------------------------------------|------------------------------------------------------------|
| Block a regulated keyword for one agent only            | Agent-version blocklist via Task 1129 (`agents/<name>/v<n>/rai.json`) |
| Tighten severity for one agent (e.g., Low-block Violence) | Agent-level RAI (Task 1129)                              |
| Enforce a domain rule the model cannot reliably learn   | Custom LangChain middleware in front of / after the model  |
| Enforce a tool-call schema (e.g., never call `transfer_funds` from text input) | Tool allow-list + custom middleware             |
| Post-check the output (groundedness, citation present)  | Output post-processor + evaluator                         |

### Step-by-step - add a custom guardrail to one agent

1. **Add an agent-level blocklist / severity** (Task 1129)
   * Edit `agents/investment-advisor/v3/rai.json` to tighten severity and add a per-agent blocklist (e.g., `fin-advisor-strict`).
   * The pipeline applies this on the next agent-version build. Deployment policy is unchanged.

2. **Write a custom LangChain middleware**
   * Create `app/guardrails/advisor_rules.py`:

     ```python
     from typing import Any
     from langchain_core.callbacks import BaseCallbackHandler
     import re

     PROMISSORY = re.compile(r"\b(guaranteed|risk[- ]?free|sure[- ]?thing|definitely (will|'ll) return)\b", re.I)

     class AdvisorPromissoryGuard(BaseCallbackHandler):
         """Blocks promissory language in advisor responses (FINRA Rule 2210)."""

         def on_llm_end(self, response: Any, **kwargs: Any) -> None:
             text = response.generations[0][0].text
             if PROMISSORY.search(text):
                 # Log to AppGuardrailEvents
                 raise GuardrailViolation(
                     category="promissory_language",
                     rule="FINRA-2210",
                     snippet=PROMISSORY.search(text).group(0),
                 )
     ```

3. **Compose the agent factory** so the custom guard sits **after** the shared moderation middleware:

   ```python
   from app.guardrails.middleware import build_moderation
   from app.guardrails.advisor_rules import AdvisorPromissoryGuard

   def build_investment_advisor(project_endpoint: str):
       moderation = build_moderation(project_endpoint)        # shared - Task 1132
       advisor_guard = AdvisorPromissoryGuard()                # custom - this agent only
       return build_agent(
           model="gpt-5.2",
           system_prompt=ADVISOR_SYSTEM_PROMPT,
           tools=ADVISOR_TOOLS,                                # tool allow-list
           middleware=[moderation, advisor_guard],
       )
   ```

4. **Restrict tools** for that agent in `ADVISOR_TOOLS`. Never include high-risk tools (`transfer_funds`, `place_trade`) unless wrapped by an explicit human-approval step.

5. **Add agent-specific test cases** to the `guardrails-test` harness under a tag (e.g., `tag: investment-advisor`) and gate the pipeline on that subset for the agent's deployment.

6. **Emit structured logs** for every custom decision so the same Log Analytics workbook (Task 1125) shows agent-level block-rate alongside fleet-wide metrics.

7. **Version + roll back** the custom rule the same way as shared policies: bump the agent version, promote dev -> test -> prod with the eval gate, rollback by redeploying the previous agent version.

### Rules of thumb

* Custom guardrails **add** to the shared stack; they never replace it.
* Custom code lives under `app/guardrails/<agent>/` and is owned by the agent team, but reviewed by Risk + Compliance.
* Every custom rule must have a regulatory or policy citation in its docstring (e.g., `# FINRA Rule 2210`, `# SEC Reg BI`).
* Every custom rule must ship with at least 5 positive and 5 negative test cases in the harness.
* Never disable a shared layer to make a custom rule pass - if there is a conflict, raise it to the Guardrail Review Board.

---

## Summary - what to show in the deck

1. The 8-layer defense-in-depth model (one diagram).
2. The three named RAI policies and which workload each maps to.
3. **Code-level focus:** Tasks 1129, 1132, 1135 - this is where the engineering team spends most of its effort.
4. **Foundry-level focus:** Tasks 1126, 1127, 1128, 1131 - configuration on the resource.
5. **Infrastructure-level focus:** Tasks 1125, 1130, 1133, 1134 - guardrails as code, observability, lifecycle.
6. The custom-guardrail pattern (above) so any team can add agent-specific rules without forking the platform.
