"""LangGraph-backed agent provider.

Implements a minimal ReAct loop:

    user -> llm -> {final answer | tool calls} -> dispatch -> llm -> ...

State, message history, and tool results are kept in a LangGraph state
graph so we can swap to checkpointed memory (Postgres) without changing
the call sites.

The graph is intentionally small and explicit - we want this code to read
as documentation. Production would use `langgraph.prebuilt.create_react_agent`.
"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, TypedDict

from bankbuddy_shared.contracts.agent import (
    AgentInvokeRequest,
    AgentInvokeResponse,
    ToolCall,
)
from bankbuddy_shared.interfaces.agent import AgentError, IAgentProvider
from bankbuddy_shared.interfaces.banking import IBankingService
from bankbuddy_shared.interfaces.llm import ILLMClient, LLMError

from ..guardrails_client import RemoteGuardrailPipeline as GuardrailPipeline
from ..tools import TOOL_SCHEMAS, BankingToolDispatcher

log = logging.getLogger("agent.langgraph")

SYSTEM_PROMPT = """You are BankBuddy, a careful banking assistant.

Rules:
- Use the provided tools to answer any question about the user's accounts,
  transactions, cards, ATMs, or loan eligibility.
- Never invent account numbers, balances, or transaction details.
- For transfers and card blocks, confirm the amount/reason in your reply
  AFTER the tool succeeds. If the tool returns an error, explain it plainly.
- Keep replies short and bank-grade professional.
"""

MAX_TOOL_HOPS = 6


class _State(TypedDict, total=False):
    messages: list[dict[str, Any]]
    user_id: str
    tools_used: list[ToolCall]


class LangGraphAgent(IAgentProvider):
    def __init__(
        self,
        llm: ILLMClient,
        banking: IBankingService,
        *,
        guardrails: GuardrailPipeline | None = None,
        block_message: str = "I'm sorry, I can't help with that request.",
        system_prompt: str | None = None,
    ) -> None:
        self._llm = llm
        self._tools = BankingToolDispatcher(banking)
        self._guardrails = guardrails
        self._block_message = block_message
        # Allow callers (factory) to override the built-in banking prompt.
        self._system_prompt = system_prompt or SYSTEM_PROMPT

    async def invoke(self, request: AgentInvokeRequest) -> AgentInvokeResponse:
        try:
            user_message = request.message
            guard_trace: dict[str, Any] = {}

            # ---- ① API_INPUT guardrails (heaviest, request just arrived) ----
            if self._guardrails is not None:
                api_in = await self._guardrails.check_api_input(
                    user_message,
                    context={"session_id": request.session_id, "subject": request.principal.subject},
                )
                if api_in.checks:
                    guard_trace["api_input"] = _serialize_pipeline_result(api_in)
                if not api_in.allowed:
                    return AgentInvokeResponse(
                        session_id=request.session_id,
                        reply=self._block_message,
                        tool_calls=[],
                        metadata={
                            "blocked": True,
                            "blocked_at": "api_input",
                            "block_reasons": api_in.block_reasons,
                            "block_categories": api_in.block_categories,
                            "guardrails": guard_trace,
                        },
                    )
                user_message = api_in.sanitized_text

            # ---- ② INPUT (LLM input) guardrails ----
            if self._guardrails is not None:
                inp = await self._guardrails.check_input(
                    user_message,
                    context={"session_id": request.session_id, "subject": request.principal.subject},
                )
                guard_trace["input"] = _serialize_pipeline_result(inp)
                if not inp.allowed:
                    return AgentInvokeResponse(
                        session_id=request.session_id,
                        reply=self._block_message,
                        tool_calls=[],
                        metadata={
                            "blocked": True,
                            "blocked_at": "input",
                            "block_reasons": inp.block_reasons,
                            "block_categories": inp.block_categories,
                            "guardrails": guard_trace,
                        },
                    )
                user_message = inp.sanitized_text

            messages = self._initial_messages(request, user_message)
            tools_called: list[ToolCall] = []

            for hop in range(MAX_TOOL_HOPS):
                resp = await self._llm.complete(
                    messages=messages,
                    tools=TOOL_SCHEMAS,
                    temperature=0.1,
                )
                msg = self._extract_message(resp)
                messages.append(msg)

                tool_calls = msg.get("tool_calls") or []
                if not tool_calls:
                    final_reply = msg.get("content") or ""
                    out_metadata: dict[str, Any] = {"guardrails": guard_trace} if guard_trace else {}

                    # ---- OUTPUT guardrails ----
                    if self._guardrails is not None:
                        outp = await self._guardrails.check_output(
                            final_reply,
                            context={"session_id": request.session_id, "subject": request.principal.subject},
                        )
                        guard_trace["output"] = _serialize_pipeline_result(outp)
                        out_metadata["guardrails"] = guard_trace
                        if not outp.allowed:
                            return AgentInvokeResponse(
                                session_id=request.session_id,
                                reply=self._block_message,
                                tool_calls=tools_called,
                                metadata={
                                    "blocked": True,
                                    "blocked_at": "output",
                                    "block_reasons": outp.block_reasons,
                                    "block_categories": outp.block_categories,
                                    "guardrails": guard_trace,
                                },
                            )
                        final_reply = outp.sanitized_text

                    # ---- ⑦ API_OUTPUT guardrails (last hop before client) ----
                    if self._guardrails is not None:
                        api_out = await self._guardrails.check_api_output(
                            final_reply,
                            context={"session_id": request.session_id, "subject": request.principal.subject},
                        )
                        if api_out.checks:
                            guard_trace["api_output"] = _serialize_pipeline_result(api_out)
                            out_metadata["guardrails"] = guard_trace
                        if not api_out.allowed:
                            return AgentInvokeResponse(
                                session_id=request.session_id,
                                reply=self._block_message,
                                tool_calls=tools_called,
                                metadata={
                                    "blocked": True,
                                    "blocked_at": "api_output",
                                    "block_reasons": api_out.block_reasons,
                                    "block_categories": api_out.block_categories,
                                    "guardrails": guard_trace,
                                },
                            )
                        final_reply = api_out.sanitized_text

                    return AgentInvokeResponse(
                        session_id=request.session_id,
                        reply=final_reply,
                        tool_calls=tools_called,
                        metadata=out_metadata,
                    )

                for tc in tool_calls:
                    fn = tc["function"]
                    name = fn["name"]
                    raw_args = fn.get("arguments") or "{}"
                    log.info("tool_call hop=%d name=%s", hop, name)

                    # ---- ④ TOOL_INPUT guardrails (planned tool call) ----
                    # We feed the guard the JSON shape {tool, arguments} so PII /
                    # secret / oversize / task-adherence guards can inspect both
                    # the tool name and the arguments before any external call.
                    if self._guardrails is not None:
                        tin_payload = json.dumps({"tool": name, "arguments": _safe_json_load(raw_args)})
                        tin = await self._guardrails.check_tool_input(
                            tin_payload,
                            context={
                                "session_id": request.session_id,
                                "subject": request.principal.subject,
                                "tool_name": name,
                                "hop": hop,
                            },
                        )
                        if tin.checks:
                            guard_trace.setdefault("tool_inputs", []).append(
                                {
                                    "tool_name": name,
                                    "hop": hop,
                                    **_serialize_pipeline_result(tin),
                                }
                            )
                        if not tin.allowed:
                            blocked_marker = json.dumps(
                                {
                                    "error": "tool_input_blocked_by_guardrails",
                                    "tool": name,
                                    "reasons": tin.block_reasons,
                                    "categories": tin.block_categories,
                                    "message": (
                                        "The planned tool call was withheld by "
                                        "guardrails policy. Apologize to the user "
                                        "and offer to retry without sensitive data."
                                    ),
                                }
                            )
                            try:
                                args_dict = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                            except json.JSONDecodeError:
                                args_dict = {"_raw": raw_args}
                            tools_called.append(
                                ToolCall(
                                    name=name,
                                    arguments=args_dict,
                                    result={
                                        "_blocked_by_guardrails": True,
                                        "stage": "tool_input",
                                        "reasons": tin.block_reasons,
                                        "categories": tin.block_categories,
                                    },
                                )
                            )
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tc.get("id", ""),
                                    "name": name,
                                    "content": blocked_marker,
                                }
                            )
                            log.warning(
                                "tool_input BLOCK hop=%d tool=%s reasons=%s",
                                hop, name, tin.block_reasons,
                            )
                            continue

                    output_json = await self._tools.call(name, raw_args, user_id=request.principal.subject)
                    try:
                        args_dict = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                    except json.JSONDecodeError:
                        args_dict = {"_raw": raw_args}
                    try:
                        result_obj: Any = json.loads(output_json)
                    except json.JSONDecodeError:
                        result_obj = output_json

                    # ---- TOOL_OUTPUT guardrails ----
                    # Run guards against the raw JSON before it is fed back
                    # to the LLM. A BLOCK does not abort the conversation:
                    # we replace the tool content with a sanitized error
                    # marker and let the LLM react. SANITIZE replaces the
                    # text with the cleaned version.
                    tool_content_for_llm = output_json
                    if self._guardrails is not None:
                        tcheck = await self._guardrails.check_tool_output(
                            output_json,
                            context={
                                "session_id": request.session_id,
                                "subject": request.principal.subject,
                                "tool_name": name,
                                "hop": hop,
                            },
                        )
                        if tcheck.checks:
                            guard_trace.setdefault("tool_outputs", []).append(
                                {
                                    "tool_name": name,
                                    "hop": hop,
                                    **_serialize_pipeline_result(tcheck),
                                }
                            )
                        if not tcheck.allowed:
                            reason_str = "; ".join(tcheck.block_reasons) or "blocked by policy"
                            tool_content_for_llm = json.dumps(
                                {
                                    "error": "tool_result_blocked_by_guardrails",
                                    "tool": name,
                                    "reasons": tcheck.block_reasons,
                                    "categories": tcheck.block_categories,
                                    "message": (
                                        "The tool returned data that was withheld "
                                        f"by guardrails policy: {reason_str}. "
                                        "Apologize to the user and offer to retry "
                                        "without the offending arguments."
                                    ),
                                }
                            )
                            log.warning(
                                "tool_output BLOCK hop=%d tool=%s reasons=%s",
                                hop, name, tcheck.block_reasons,
                            )
                            # Surface block details on the recorded ToolCall
                            # so the UI / clients can see the suppression.
                            result_obj = {
                                "_blocked_by_guardrails": True,
                                "reasons": tcheck.block_reasons,
                                "categories": tcheck.block_categories,
                            }
                        elif tcheck.was_modified:
                            tool_content_for_llm = tcheck.sanitized_text
                            log.info(
                                "tool_output SANITIZE hop=%d tool=%s",
                                hop, name,
                            )

                    tools_called.append(
                        ToolCall(
                            name=name,
                            arguments=args_dict,
                            result=result_obj,
                        )
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "name": name,
                            "content": tool_content_for_llm,
                        }
                    )

            raise AgentError(f"tool-call budget exhausted ({MAX_TOOL_HOPS} hops)")
        except LLMError as e:
            raise AgentError(str(e)) from e

    async def stream(self, request: AgentInvokeRequest) -> AsyncIterator[str]:
        # Phase 1c: token streaming is a stretch goal; emit the final reply as one chunk.
        result = await self.invoke(request)
        yield result.reply

    def _initial_messages(self, request: AgentInvokeRequest, user_message: str) -> list[dict[str, Any]]:
        return [
            {"role": "system", "content": self._system_prompt},
            {
                "role": "system",
                "content": (
                    f"Current customer id: {request.principal.subject}. "
                    f"Customer name: {request.principal.username or 'unknown'}."
                ),
            },
            {"role": "user", "content": user_message},
        ]

    @staticmethod
    def _extract_message(resp: dict[str, Any]) -> dict[str, Any]:
        try:
            return dict(resp["choices"][0]["message"])
        except (KeyError, IndexError, TypeError) as e:
            raise AgentError(f"malformed LLM response: {resp!r}") from e


def _serialize_pipeline_result(result: Any) -> dict[str, Any]:
    return {
        "stage": result.stage.value if hasattr(result.stage, "value") else str(result.stage),
        "allowed": result.allowed,
        "duration_ms": round(result.duration_ms, 2),
        "checks": [
            {
                "guard": c.guard_name,
                "decision": c.decision.value,
                "reasons": c.reasons,
                "categories": c.categories,
                "score": c.score,
                "metadata": c.metadata,
            }
            for c in result.checks
        ],
    }


def _safe_json_load(raw: Any) -> Any:
    """Best-effort JSON parse for tool arguments; never raises."""
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str):
        return {"_raw": str(raw)}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw}
