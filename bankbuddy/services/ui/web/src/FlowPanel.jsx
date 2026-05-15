// FlowPanel: shows the end-to-end function-call flow for the latest /chat
// request, naming each function and the file it lives in.
//
// Static skeleton (UI -> API -> Agent -> guardrails -> LLM -> tools -> guardrails -> reply)
// is enriched with dynamic data pulled from `trace`:
//   - dynamic guard list (input + output stages)
//   - dynamic tool calls (banking_tools dispatcher + IBankingService method)
//
// The intent is *teaching*: every row shows `function_name (file.py)` so a
// reader can jump straight to the source.
import React from 'react';

// Map each tool name -> (dispatcher branch, downstream IBankingService method).
// All tool dispatcher branches live in `BankingToolDispatcher._dispatch` in
// `services/agent/app/tools/banking_tools.py`. The downstream service method
// lives in `services/api/app/services/banking_service.py` (or the mock-bank).
const TOOL_TO_SERVICE = {
  get_accounts: 'MockBankHttpClient.get_accounts',
  get_transactions: 'MockBankHttpClient.get_transactions',
  transfer: 'MockBankHttpClient.transfer',
  block_card: 'MockBankHttpClient.block_card',
  find_atms: 'MockBankHttpClient.find_atms',
  check_loan_eligibility: 'MockBankHttpClient.check_loan_eligibility',
};

// Each guard's source file (relative to services/guardrails/app/core/).
// Guards now live in the standalone guardrails service, not the agent.
const GUARD_FILES = {
  'token-limit': 'guards/token_limit.py',
  'banned-substrings': 'guards/banned_substrings.py',
  'prompt-injection': 'guards/prompt_injection.py',
  'pii-detect': 'guards/pii_detect.py',
  'banking-relevance': 'guards/banking_relevance.py',
  'output-pii-redact': 'guards/output_pii_redact.py',
  'secret-leak': 'guards/secret_leak.py',
  toxicity: 'guards/toxicity.py',
  'competitor-mentions': 'guards/competitor_mentions.py',
  'azure-content-safety': 'guards/azure_content_safety.py',
  'azure-pii-detection': 'guards/azure_pii_detection.py',
  'response-shape': 'guards/response_shape.py',
  groundedness: 'guards/groundedness.py',
  'task-adherence': 'guards/task_adherence.py',
  'bias-detect': 'guards/bias_detect.py',
};

const GUARDRAILS_BASE = 'services/guardrails/app/core/';

function FnRow({ idx, fn, file, decision, note, depth = 0, blocked }) {
  const cls = ['flow-row'];
  if (decision === 'block') cls.push('blocked');
  if (decision === 'allow') cls.push('allowed');
  if (blocked) cls.push('blocked');
  return (
    <div className={cls.join(' ')} style={{ marginLeft: depth * 18 }}>
      <span className="flow-idx">{idx}</span>
      <span className="flow-fn"><code>{fn}</code></span>
      <span className="flow-file">({file})</span>
      {decision && (
        <span className={`pill ${decision === 'block' ? 'block' : 'ok'}`}>{decision}</span>
      )}
      {note && <span className="flow-note">{note}</span>}
    </div>
  );
}

function Section({ title, children }) {
  return (
    <div className="flow-section">
      <div className="flow-section-title">{title}</div>
      <div className="flow-rows">{children}</div>
    </div>
  );
}

export default function FlowPanel({ trace, busy, lastUser }) {
  if (!trace && !busy) {
    return (
      <div className="flow-empty">
        Send a message to see the end-to-end function call path. Each step lists the
        function name and the file it lives in.
      </div>
    );
  }
  if (busy && !trace) {
    return <div className="flow-empty">Tracing request...</div>;
  }

  const inputChecks = trace?.guardrails?.input?.checks || [];
  const outputChecks = trace?.guardrails?.output?.checks || [];
  const toolOutputRuns = trace?.guardrails?.tool_outputs || [];
  const toolCalls = trace?.tool_calls || [];
  const blockedAt = trace?.blocked_at; // 'input' | 'output' | null
  const blocked = !!trace?.blocked;

  let i = 0;
  const next = () => ++i;

  const llmReached = blockedAt !== 'input';
  const outputReached = blockedAt !== 'input';

  return (
    <div className="flow-list">
      {lastUser && (
        <div className="flow-user">
          <strong>User:</strong> <span>{lastUser}</span>
        </div>
      )}

      <Section title="UI (browser)">
        <FnRow idx={next()} fn="send()" file="src/Chat.jsx" />
        <FnRow idx={next()} fn="api.chat()" file="src/api.js" note="POST /chat" />
      </Section>

      <Section title="API service (FastAPI)">
        <FnRow idx={next()} fn="chat()" file="services/api/app/routers.py" note="auth + session" />
        <FnRow idx={next()} fn="AgentClient.invoke()" file="services/api/app/agent_client.py" note="httpx -> agent:8100/invoke" />
      </Section>

      <Section title="Agent service (FastAPI + LangGraph)">
        <FnRow idx={next()} fn="invoke()" file="services/agent/app/routers.py" />
        <FnRow idx={next()} fn="LangGraphProvider.invoke()" file="services/agent/app/providers/langgraph_provider.py" />
      </Section>

      <Section title={`Input guardrails${blockedAt === 'input' ? ' — BLOCKED' : ''}`}>
        <FnRow idx={next()} fn="RemoteGuardrailPipeline.run(stage='input')" file="services/agent/app/guardrails_client.py" note="httpx -> guardrails:8001/v1/check (bearer)" />
        <FnRow idx={next()} fn="check()" file="services/guardrails/app/main.py" depth={1} note="FastAPI route POST /v1/check" />
        <FnRow idx={next()} fn="GuardPipeline.run(stage='input')" file={`${GUARDRAILS_BASE}pipeline.py`} depth={1} />
        {inputChecks.map((c) => (
          <FnRow
            key={`in-${c.guard}`}
            idx={next()}
            fn={`${classFor(c.guard)}.check()`}
            file={`${GUARDRAILS_BASE}${GUARD_FILES[c.guard] || 'guards/?.py'}`}
            decision={c.decision}
            note={
              c.decision === 'block'
                ? (c.categories || []).join(', ') || 'blocked'
                : null
            }
            depth={2}
          />
        ))}
      </Section>

      {llmReached && (
        <Section title="LLM + tools">
          <FnRow idx={next()} fn="LiteLLMClient.complete()" file="services/agent/app/llm/litellm_client.py" note="model + auth" />
          <FnRow idx={next()} fn="LLMAuthProvider.apply()" file="services/agent/app/llm/auth.py" note="StaticBearerAuth | ApiKeyAuth | AzureAdAuth" depth={1} />
          {toolCalls.length === 0 && (
            <FnRow idx={next()} fn="(no tools called)" file="-" note="LLM answered directly" depth={1} />
          )}
          {(() => {
            const seen = new Map();
            const rows = [];
            toolCalls.forEach((tc, idx) => {
              const n = seen.get(tc.name) || 0;
              seen.set(tc.name, n + 1);
              const matching = toolOutputRuns.filter((g) => g.tool_name === tc.name)[n];
              const toolBlocked = !!tc.result?._blocked_by_guardrails;
              rows.push(
                <FnRow
                  key={`tc-${idx}`}
                  idx={next()}
                  fn={`BankingToolDispatcher.call(name='${tc.name}')`}
                  file="services/agent/app/tools/banking_tools.py"
                  depth={1}
                  note={summarizeArgs(tc.arguments)}
                  blocked={toolBlocked}
                />
              );
              if (TOOL_TO_SERVICE[tc.name]) {
                rows.push(
                  <FnRow
                    key={`tcs-${idx}`}
                    idx={next()}
                    fn={`${TOOL_TO_SERVICE[tc.name]}()`}
                    file="services/agent/app/banking/mock_http.py"
                    depth={2}
                    note={toolBlocked ? 'result withheld by guardrails' : summarizeResult(tc.result)}
                  />
                );
              }
              if (matching && matching.checks?.length > 0) {
                rows.push(
                  <FnRow
                    key={`tg-pipeline-${idx}`}
                    idx={next()}
                    fn={`RemoteGuardrailPipeline.check_tool_output(tool='${tc.name}')`}
                    file="services/agent/app/guardrails_client.py"
                    depth={1}
                    note={`httpx -> guardrails:8001/v1/check (stage=tool_output) — ${matching.duration_ms} ms`}
                    decision={matching.allowed ? 'allow' : 'block'}
                  />
                );
                rows.push(
                  <FnRow
                    key={`tg-route-${idx}`}
                    idx={next()}
                    fn="check()"
                    file="services/guardrails/app/main.py"
                    depth={2}
                    note="FastAPI route POST /v1/check (tool_output)"
                  />
                );
                rows.push(
                  <FnRow
                    key={`tg-pl-${idx}`}
                    idx={next()}
                    fn="GuardPipeline.check_tool_output()"
                    file={`${GUARDRAILS_BASE}pipeline.py`}
                    depth={2}
                  />
                );
                matching.checks.forEach((c, j) => {
                  rows.push(
                    <FnRow
                      key={`tg-${idx}-${j}-${c.guard}`}
                      idx={next()}
                      fn={`${classFor(c.guard)}.check()`}
                      file={`${GUARDRAILS_BASE}${GUARD_FILES[c.guard] || 'guards/?.py'}`}
                      decision={c.decision}
                      note={
                        c.decision === 'block'
                          ? (c.categories || []).join(', ') || 'blocked'
                          : null
                      }
                      depth={3}
                    />
                  );
                });
                if (toolBlocked) {
                  rows.push(
                    <FnRow
                      key={`tg-replace-${idx}`}
                      idx={next()}
                      fn="(tool result replaced with blocked-marker JSON)"
                      file="services/agent/app/providers/langgraph_provider.py"
                      depth={2}
                      note="LLM sees error stub instead of original tool data"
                    />
                  );
                }
              }
            });
            return rows;
          })()}
        </Section>
      )}

      {outputReached && (
        <Section title={`Output guardrails${blockedAt === 'output' ? ' — BLOCKED' : ''}`}>
          <FnRow idx={next()} fn="RemoteGuardrailPipeline.run(stage='output')" file="services/agent/app/guardrails_client.py" note="httpx -> guardrails:8001/v1/check (bearer)" />
          <FnRow idx={next()} fn="check()" file="services/guardrails/app/main.py" depth={1} note="FastAPI route POST /v1/check" />
          <FnRow idx={next()} fn="GuardPipeline.run(stage='output')" file={`${GUARDRAILS_BASE}pipeline.py`} depth={1} />
          {outputChecks.map((c) => (
            <FnRow
              key={`out-${c.guard}`}
              idx={next()}
              fn={`${classFor(c.guard)}.check()`}
              file={`${GUARDRAILS_BASE}${GUARD_FILES[c.guard] || 'guards/?.py'}`}
              decision={c.decision}
              note={
                c.decision === 'block'
                  ? (c.categories || []).join(', ') || 'blocked'
                  : null
              }
              depth={2}
            />
          ))}
        </Section>
      )}

      <Section title="Reply">
        <FnRow
          idx={next()}
          fn={blocked ? 'AgentResponse(blocked=True)' : 'AgentResponse(reply=...)'}
          file="services/agent/app/providers/langgraph_provider.py"
          decision={blocked ? 'block' : 'allow'}
        />
        <FnRow idx={next()} fn="ChatResponse(...)" file="services/api/app/routers.py" note="returns reply + trace" />
        <FnRow idx={next()} fn="setMessages() / setTrace()" file="src/Chat.jsx" />
      </Section>
    </div>
  );
}

// Convert "banking-relevance" -> "BankingRelevanceGuard" for display.
// Special-case the Azure-prefixed guards so they read naturally.
function classFor(name) {
  const overrides = {
    'azure-content-safety': 'AzureContentSafetyGuard',
    'azure-pii-detection': 'AzurePiiDetectionGuard',
  };
  if (overrides[name]) return overrides[name];
  const camel = (name || '')
    .split(/[-_]/)
    .map((s) => s.charAt(0).toUpperCase() + s.slice(1))
    .join('');
  return `${camel}Guard`;
}

function summarizeArgs(args) {
  if (!args || typeof args !== 'object') return null;
  const keys = Object.keys(args);
  if (!keys.length) return 'args: {}';
  const head = keys
    .slice(0, 3)
    .map((k) => {
      const v = args[k];
      const s = typeof v === 'string' ? `'${v}'` : JSON.stringify(v);
      return `${k}=${s}`;
    })
    .join(', ');
  return `args: ${head}${keys.length > 3 ? ', ...' : ''}`;
}

function summarizeResult(result) {
  if (result == null) return null;
  if (typeof result === 'string') {
    const t = result.length > 70 ? result.slice(0, 70) + '...' : result;
    return `result: ${t}`;
  }
  try {
    const t = JSON.stringify(result);
    return `result: ${t.length > 70 ? t.slice(0, 70) + '...' : t}`;
  } catch {
    return 'result: ...';
  }
}
