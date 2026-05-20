import { useState } from 'react';

/**
 * Right-side panel that visualizes the end-to-end request flow:
 *
 *   user message
 *      |
 *      v
 *   [INPUT GUARDRAILS]  (per-guard pass/block/warn)
 *      |
 *      v  (only if not blocked)
 *   [LLM + tool calls]
 *      |
 *      v
 *   [OUTPUT GUARDRAILS]
 *      |
 *      v
 *   reply to user
 *
 * Data shape (from /chat -> trace):
 *   {
 *     blocked, blocked_at, block_reasons[], block_categories[],
 *     guardrails: {
 *       input?:  { stage, allowed, duration_ms, checks: [{guard, decision, reasons[], categories[], score}] }
 *       output?: { ...same shape... }
 *     },
 *     tool_calls: [{name, arguments, result}]
 *   }
 */
export default function TracePanel({ trace, lastUserMessage, lastReply, busy }) {
  const hasTrace = trace && (trace.guardrails || trace.tool_calls?.length);

  return (
    <aside className="card trace">
      <h3>
        <span>Request flow</span>
        {trace?.blocked && <span className="pill block">BLOCKED at {trace.blocked_at}</span>}
        {!trace?.blocked && hasTrace && <span className="pill ok">ALLOWED</span>}
      </h3>

      {!hasTrace && !busy && (
        <p className="empty">Send a message to see the live request flow, guardrail decisions, and tool calls.</p>
      )}
      {busy && <p className="empty">running pipeline...</p>}

      {hasTrace && (
        <>
          <UserStep text={lastUserMessage} />

          <Arrow label="① api_input guardrails" />
          <StageView
            title="① API_INPUT guardrails"
            stage={trace.guardrails?.api_input}
            stageKey="api_input"
            blocked={trace.blocked && trace.blocked_at === 'api_input'}
          />

          <Arrow label="② input (LLM input) guardrails" />
          <StageView
            title="② INPUT (LLM input) guardrails"
            stage={trace.guardrails?.input}
            stageKey="input"
            blocked={trace.blocked && trace.blocked_at === 'input'}
          />

          <Arrow label="④ LLM + tools (with ④ tool_input / ⑤ tool_output guardrails)" />
          <ToolsView
            tools={trace.tool_calls || []}
            toolInputs={trace.guardrails?.tool_inputs || []}
            toolOutputs={trace.guardrails?.tool_outputs || []}
            blockedBefore={trace.blocked && ['api_input', 'input'].includes(trace.blocked_at)}
          />

          <Arrow label="③ output (LLM output) guardrails" />
          <StageView
            title="③ OUTPUT (LLM output) guardrails"
            stage={trace.guardrails?.output}
            stageKey="output"
            blocked={trace.blocked && trace.blocked_at === 'output'}
          />

          <Arrow label="⑥ api_output guardrails" />
          <StageView
            title="⑥ API_OUTPUT guardrails"
            stage={trace.guardrails?.api_output}
            stageKey="api_output"
            blocked={trace.blocked && trace.blocked_at === 'api_output'}
          />

          <Arrow label="reply" />
          <ReplyStep text={lastReply} blocked={trace.blocked} />

          <RawJson trace={trace} />
        </>
      )}
    </aside>
  );
}

function UserStep({ text }) {
  return (
    <div className="stage">
      <h4>User message</h4>
      <div style={{ fontSize: 12, color: 'var(--muted)', wordBreak: 'break-word' }}>
        {text || '(none)'}
      </div>
    </div>
  );
}

function ReplyStep({ text, blocked }) {
  return (
    <div className="stage">
      <h4>
        Reply
        {blocked && <span className="pill block">BLOCKED</span>}
      </h4>
      <div style={{ fontSize: 12, color: 'var(--muted)', wordBreak: 'break-word' }}>
        {text || '(none)'}
      </div>
    </div>
  );
}

function Arrow({ label }) {
  return (
    <div className="flow-arrow">
      <span>v</span>
      <span className="label">{label}</span>
    </div>
  );
}

function StageView({ title, stage, stageKey, blocked }) {
  if (!stage) {
    return (
      <div className="stage">
        <h4>
          <span>
            {title}
            {' '}
            <span className="pill muted">not run</span>
          </span>
        </h4>
        <div className="empty">no guards configured at this stage (toggle <code>enabled: true</code> in <code>default.yaml</code>)</div>
      </div>
    );
  }
  return (
    <div className="stage">
      <h4>
        <span>
          {title}
          {' '}
          <span className={`pill ${stage.allowed ? 'ok' : 'block'}`}>
            {stage.allowed ? 'pass' : 'block'}
          </span>
        </span>
        <span className="duration">{stage.duration_ms} ms</span>
      </h4>
      {(stage.checks || []).length === 0 && <div className="empty">no checks ran</div>}
      {(stage.checks || []).map((c, i) => (
        <GuardRow key={`${stageKey}-${i}-${c.guard}`} check={c} />
      ))}
    </div>
  );
}

function GuardRow({ check }) {
  const decision = (check.decision || '').toLowerCase();
  const icon = decision === 'allow' ? '+' : decision === 'block' ? 'x' : '!';
  const pillClass =
    decision === 'allow' ? 'ok' :
    decision === 'block' ? 'block' :
    decision === 'modify' ? 'warn' : 'muted';
  const categoryResults = check.metadata?.category_results || [];
  const threshold = check.metadata?.threshold;
  return (
    <div className="guard-row">
      <span className="icon">{icon}</span>
      <span className="name">{check.guard}</span>
      <span className={`pill ${pillClass}`}>{check.decision}</span>
      {categoryResults.length > 0 && (
        <span className="cat-grid">
          {categoryResults.map((cr) => {
            const pillCls = cr.skipped ? 'muted' : cr.passed ? 'ok' : 'block';
            const mark = cr.skipped ? '~' : cr.passed ? '+' : 'x';
            const sev = typeof cr.severity === 'number' ? ` (${cr.severity})` : '';
            const tip = cr.skipped
              ? `skipped: ${cr.reason || 'not configured'}`
              : typeof cr.severity === 'number'
                ? `severity ${cr.severity}${threshold != null ? ` / threshold ${threshold}` : ''}`
                : undefined;
            return (
              <span key={cr.category} className={`pill ${pillCls}`} title={tip}>
                {mark} {cr.category}{sev}
              </span>
            );
          })}
        </span>
      )}
      {(check.reasons?.length || check.categories?.length) ? (
        <span className="reasons">
          {check.categories?.length ? `[${check.categories.join(', ')}] ` : ''}
          {check.reasons?.join('; ')}
          {typeof check.score === 'number' ? ` (score ${check.score.toFixed(2)})` : ''}
        </span>
      ) : null}
    </div>
  );
}

function ToolsView({ tools, toolInputs, toolOutputs, blockedBefore }) {
  if (blockedBefore) {
    return (
      <div className="stage tools">
        <h4>LLM / tools</h4>
        <div className="empty">skipped (blocked at api_input/input)</div>
      </div>
    );
  }
  // Track per-(name) hop counter so we can match tool_calls (which have no
  // hop field) to the recorded guard runs in arrival order.
  const seenCount = new Map();
  return (
    <div className="stage tools">
      <h4>
        <span>LLM + tools</span>
        <span className="duration">
          {tools.length} tool call{tools.length === 1 ? '' : 's'}
        </span>
      </h4>
      {tools.length === 0 && <div className="empty">no tools called</div>}
      {tools.map((t, i) => {
        const seen = seenCount.get(t.name) || 0;
        seenCount.set(t.name, seen + 1);
        const matchingIn = (toolInputs || []).filter((g) => g.tool_name === t.name)[seen];
        const matching = (toolOutputs || []).filter((g) => g.tool_name === t.name)[seen];
        const blocked = !!t.result?._blocked_by_guardrails;
        const blockedAtToolInput = t.result?.stage === 'tool_input';
        return (
          <div key={i} className="tool">
            <div>
              <span className="tname">{t.name}</span>
              {blockedAtToolInput && (
                <span className="pill block" style={{ marginLeft: 6 }}>tool call BLOCKED at tool_input</span>
              )}
              {blocked && !blockedAtToolInput && (
                <span className="pill block" style={{ marginLeft: 6 }}>tool result BLOCKED</span>
              )}
            </div>
            {matchingIn && matchingIn.checks?.length > 0 && (
              <div className="tool-guard-block">
                <div className="tool-guard-title">
                  ④ tool-input guardrails{' '}
                  <span className={`pill ${matchingIn.allowed ? 'ok' : 'block'}`}>
                    {matchingIn.allowed ? 'pass' : 'block'}
                  </span>{' '}
                  <span className="duration">{matchingIn.duration_ms} ms</span>
                </div>
                {matchingIn.checks.map((c, j) => (
                  <GuardRow key={`ti-${i}-${j}-${c.guard}`} check={c} />
                ))}
              </div>
            )}
            {matching && matching.checks?.length > 0 && (
              <div className="tool-guard-block">
                <div className="tool-guard-title">
                  ⑤ tool-output guardrails{' '}
                  <span className={`pill ${matching.allowed ? 'ok' : 'block'}`}>
                    {matching.allowed ? 'pass' : 'block'}
                  </span>{' '}
                  <span className="duration">{matching.duration_ms} ms</span>
                </div>
                {matching.checks.map((c, j) => (
                  <GuardRow key={`tg-${i}-${j}-${c.guard}`} check={c} />
                ))}
              </div>
            )}
            <details>
              <summary>args / result</summary>
              <pre>{JSON.stringify({ arguments: t.arguments, result: t.result }, null, 2)}</pre>
            </details>
          </div>
        );
      })}
    </div>
  );
}

function RawJson({ trace }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="raw">
      <details open={open} onToggle={(e) => setOpen(e.target.open)}>
        <summary>raw trace JSON</summary>
        <pre>{JSON.stringify(trace, null, 2)}</pre>
      </details>
    </div>
  );
}
