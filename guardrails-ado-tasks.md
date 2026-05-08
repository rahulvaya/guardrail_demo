# Guardrails - ADO Tasks

Four tasks covering the guardrails capabilities and design patterns implemented in this work stream. Copy each block into its own ADO Task work item.

---

## Task 1

**Task title:** Guardrails: Pluggable guard framework with Strategy + Registry + Pipeline patterns

**Task details:**

- Goal: a provider-agnostic, cloud-agnostic guardrails framework where every guard is a self-contained, hot-swappable unit governed by configuration.
- **Design patterns applied:**
  - **Strategy:** every guard implements a common `IGuard` interface (`name`, `stage`, `validate(context) -> GuardResult`). Each guard encapsulates one policy; callers depend only on the interface.
  - **Registry:** central `registry.py` discovers guards, reads `GUARDRAILS_ENABLED` (master) and per-guard `GUARD_<NAME>_ENABLED` flags, and yields the active set per stage. `_is_enabled()` honors master AND per-guard flags so any guard can be toggled at runtime via env var.
  - **Pipeline / Chain of Responsibility:** `GuardrailPipeline` runs input guards in order before the LLM and output guards after; the first hard-block short-circuits with a structured verdict.
  - **Result object:** `GuardResult { allowed, reason, redactions, metadata }` keeps verdicts uniform and serializable for tracing.
- **Implemented guards (input stage):** `token-limit`, `banned-substrings`, `prompt-injection`, `pii-detect`, `banking-relevance`.
- **Implemented guards (output stage):** `output-pii-redact`, `secret-leak`, `toxicity`, `competitor-mentions`.
- **Configuration surface:** typed Pydantic settings (`Field(alias="GUARD_<NAME>_ENABLED")`) plus tunable thresholds (e.g. relevance `min_length`) - no code changes needed to enable/disable or tune.
- **Observability:** boot log prints `guardrail <name> [stage] enabled|disabled config={...}`; per-turn each guard logs its verdict with reason.
- **Acceptance:** adding a new guard requires only (1) implementing `IGuard`, (2) registering in the registry, (3) adding a flag - zero changes to the pipeline or callers.

---

## Task 2

**Task title:** Guardrails: Defense-in-depth layering (LLM persona + programmatic guards + tool schemas)

**Task details:**

- Goal: clearly separate the three independent enforcement layers so reviewers can attribute every refusal/redaction to a specific layer and reason about coverage gaps.
- **Layer 0 - LLM persona (soft guardrail):** the system prompt scopes behavior (tone, allowed topics, refusal style). Made configurable via `AGENT_SYSTEM_PROMPT` env var so the persona can be swapped without code changes; falls back to built-in default.
  - **Pattern:** Dependency Injection - the provider factory injects `system_prompt=settings.agent_system_prompt` into the agent constructor.
- **Layer 1 - Programmatic guards (hard guardrails):** the `GuardrailPipeline` runs deterministic checks before/after the LLM. Cannot be bypassed by prompt injection because they do not trust LLM output.
- **Layer 2 - Tool schemas (capability guardrail):** the LLM can only invoke tools whose JSON schemas are registered with the dispatcher; arguments are validated before execution. This bounds *actions* even if Layers 0/1 are loose.
- **Why all three:** disabling Layer 1 alone does not unlock free-form responses because Layer 0 still constrains the LLM; this is intentional and must be documented in the guardrails docs and demo runbook.
- **Acceptance:** docs include a "which layer caught this" matrix; each layer is independently togglable in the demo; trace output identifies the layer responsible for any block/redaction.

---

## Task 3

**Task title:** Guardrails: Provider abstraction for cloud-agnostic agents (Strategy + Factory + Adapter)

**Task details:**

- Goal: the guardrails framework must not be coupled to a specific LLM vendor, banking backend, or auth scheme so it can ship across Azure OpenAI, OpenAI Assistants, Bedrock, and Foundry.
- **Design patterns applied:**
  - **Strategy (`IAgentProvider`):** `LangGraphAgent`, `FoundryAgentProvider`, `OpenAIAssistantProvider`, `BedrockAgentProvider` all implement the same provider interface; the pipeline + guards work identically against any of them.
  - **Factory (`providers/factory.py`):** `make_agent(settings, llm, banking, guardrails)` selects the provider by `AGENT_PROVIDER` env var and injects `guardrails`, `block_message`, and `system_prompt` uniformly.
  - **Adapter (`ILLMClient`):** `LiteLLMClient` adapts vendor-specific HTTP/SDK calls to a single `chat(messages, tools)` shape; guards consume normalized output regardless of vendor.
  - **Strategy (auth):** `StaticBearerAuth`, `AADTokenAuth`, etc. behind a `build_auth()` selector so AAD / API-key / managed-identity all plug in without changing the LLM client.
  - **Adapter (`IBankingService`):** `BankingToolDispatcher` wraps the concrete backend (mock vs real), so tool-schema and tool-arg guardrails run independently of the backing system.
- **Outcome:** swapping providers or auth modes is an env-var change; guards, pipeline, and trace format are unchanged.
- **Acceptance:** every guard test passes against at least two providers; the factory raises a clear error on unknown `AGENT_PROVIDER`; auth strategy is selected purely from env.

---

## Task 4

**Task title:** Guardrails: Decision tracing and observability (Decorator + DTO patterns)

**Task details:**

- Goal: every chat turn produces a structured, end-to-end execution trace so reviewers can see exactly which guard ran, its verdict, and where it lives in source.
- **Design patterns applied:**
  - **Decorator / Interceptor:** the pipeline wraps each `IGuard.validate()` call to capture `name`, `stage`, `fn`, `file`, `verdict`, `reason`, `redactions`, `duration_ms` without polluting guard logic.
  - **DTO (`FlowStep`, `GuardResult`):** uniform serializable shapes for UI/API consumption; downstream surfaces do not need to know guard internals.
  - **Composite trace:** ordered steps grouped into sections - UI -> API -> Agent -> Input guards -> LLM + tool calls -> Output guards -> Reply - each step exposes function name + source file.
  - **Visitor (UI rendering):** the `FlowPanel` component walks the trace once and renders per-section views (chat reply tab vs function-call flow tab) from the same data.
- **Block semantics:** first hard-block short-circuits with a structured `block_message` so the trace clearly distinguishes "passed", "redacted", "blocked".
- **Operator surface:** boot log lists every guard with state and config; per-turn log line per guard with verdict + reason; UI "Function calls" tab renders the same trace visually with blocked rows highlighted.
- **Acceptance:** a single chat turn yields a trace consumable by both logs and UI; reviewers can click from a blocked row to its source file; trace shape is stable across providers.
