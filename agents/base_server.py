"""Shared server that powers every specialist — built on a2a-sdk 0.3.x.

Each specialist server module just calls `run_specialist("<id>")`. The agent
implements `AgentExecutor.execute` to translate an A2A request into a call
through the LiteLLM gateway, then emits proper A2A lifecycle events
(submitted → working → status updates → final artifact → completed).

Routes wired up by the SDK (via A2AStarletteApplication):
  - GET  /.well-known/agent-card.json
  - POST /a2a                           JSON-RPC entry point

Plus our own:
  - GET  /healthz                       liveness
  - GET  /                              human-readable index
  - POST /invoke / /invoke-sync         non-A2A convenience (curl-friendly)

  LLM calls route through the shared LiteLLM proxy .
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    Part,
    TaskState,
    TextPart,
)
from sse_starlette.sse import EventSourceResponse
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.specialists import SpecialistSpec, get_spec  # noqa: E402
from config import get_settings  # noqa: E402
from common.deadline import DEFAULT_TASK_DEADLINE, DeadlineExceeded, deadline, remaining_or, effective_deadline  # noqa: E402
from common.decorations import get_decoration  # noqa: E402
from common.llm_client import make_async_client, stream_complete  # noqa: E402
from common.peer_client import (  # noqa: E402
    PEER_BREVITY_INSTRUCTION,
    PEER_MAX_TOKENS,
    consult_peer,
    is_peer_call,
    strip_peer_marker,
)
from common.retry import with_retry  # noqa: E402
from observability.context import ExecutionContext, bind_context  # noqa: E402

from common.log import setup as _setup_logging
_setup_logging()
logger = logging.getLogger("multi_agent.agent")


def _text_part(text: str) -> Part:
    """Wrap a string into the discriminated-union `Part` shape."""
    return Part(root=TextPart(text=text))


# ---------------------------------------------------------------------------
#  Peer-consultation tool (OpenAI tool-call shape, served via LiteLLM)
# ---------------------------------------------------------------------------


_PEER_TOOL_NAME = "consult_peer"
_MAX_TOOL_ITERS = 4   # safety cap on the tool loop


def _consult_peer_tool(peers: list[str]) -> dict:
    """Build the OpenAI tools[] entry that exposes A2A peer consultation."""
    return {
        "type": "function",
        "function": {
            "name": _PEER_TOOL_NAME,
            "description": (
                "Consult another A2A specialist for their expert opinion on a "
                "sub-question, then incorporate their answer into your own. "
                "Use sparingly — only when the question genuinely needs that "
                "specialist's lens and you cannot answer it confidently alone. "
                "Each available peer's role is summarized below."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "peer_name": {
                        "type": "string",
                        "enum": peers,
                        "description": "Which peer to consult.",
                    },
                    "question": {
                        "type": "string",
                        "description": (
                            "A self-contained question for the peer. "
                            "They will not see your prior context — include "
                            "everything they need to answer."
                        ),
                    },
                },
                "required": ["peer_name", "question"],
            },
        },
    }


def _peer_hint(peers: list[str]) -> str:
    """Short description block appended to the system prompt."""
    lines = ["", "---", "## Peer consultation (A2A)", "", (
        "You can consult these peer specialists by emitting a `consult_peer` "
        "tool call. They are reachable via the LiteLLM Agent Gateway — you do not need "
        "to know their network address."
    ), ""]
    for p in peers:
        deco = get_decoration(p)
        lines.append(f"- **{p}** ({deco.role}) — {deco.icon} ask for their expertise on {', '.join(deco.expertise[:6])}")
    lines.extend([
        "",
        "Guidelines:",
        "- Only consult a peer when their lens materially improves your answer.",
        "- Frame the peer question as a focused, self-contained sub-question.",
        "- You may consult more than one peer if needed.",
        "- After receiving peer answers, weave them into YOUR final answer in your own structured format. Do not just paste them.",
    ])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
#  A2A AgentExecutor — one per specialist
# ---------------------------------------------------------------------------


class SpecialistAgentExecutor(AgentExecutor):
    """Adapter from A2A's AgentExecutor contract → our LiteLLM-routed LLM,
    with optional A2A peer-to-peer consultation via OpenAI tool calls
    It does the following on each request:
    1. Announce submission (for new requests only)
    2. Move to working status
    3. If no peers are offered OR called as a peer, run the simple streaming
    4. Otherwise, run the tool loop with consult_peer calls until the model returns content with no tool calls (final answer) or we hit the iteration cap.
    5. Append a "Consulted peers" footer if any were consulted (top-level only)
    6. Emit the final artifact + completed status.
    """

    def __init__(self, spec: SpecialistSpec) -> None:
        self.spec = spec
        self.peers = get_decoration(spec.id).peers

    @staticmethod
    def _inbound_metadata(context: RequestContext) -> dict:
        """The JSON-RPC params.metadata of the inbound request.

        a2a-sdk's RequestContext exposes the MessageSendParams metadata via
        ``context.metadata``. This is where A2AClient.build_payload puts
        ``execution_context`` and ``deadline_remaining``. We also check the
        message metadata as a fallback. (``current_task`` is None on a fresh
        request — that was the bug — so it is NOT a valid source.)
        """
        for src in (
            getattr(context, "metadata", None),
            getattr(getattr(context, "message", None), "metadata", None),
        ):
            if isinstance(src, dict) and src:
                return src
        return {}

    def _extract_exec_ctx(self, context: RequestContext) -> ExecutionContext:
        """Recover the propagated ExecutionContext, else synthesize one.

        The orchestrator embeds it via ``A2AClient.build_payload`` in
        ``params.metadata.execution_context``. If it survived the hop we
        rebuild it (so nested peer spans share the run's trace/conversation);
        otherwise we mint a synthetic root so consultations still correlate
        locally.
        """
        meta = self._inbound_metadata(context)
        ec_dict = meta.get("execution_context")
        if isinstance(ec_dict, dict):
            try:
                # The orchestrator already opened THIS agent's span (it emits
                # agent_started on the same context it propagates here), so we
                # bind it as-is — peer consults then nest directly under it.
                ctx = ExecutionContext(**ec_dict)
                logger.info(
                    "ctx.propagated agent=%s conversation=%s trace=%s span=%s",
                    self.spec.id, ctx.conversation_id[:8], ctx.trace_id[:8], ctx.span_id[:8],
                )
                return ctx
            except Exception:
                logger.warning("ctx.parse_failed agent=%s", self.spec.id, exc_info=True)
        logger.info("ctx.synthetic agent=%s (no execution_context in inbound metadata)", self.spec.id)
        return ExecutionContext.new_root(
            conversation_id=context.context_id or str(uuid.uuid4()),
            workflow_name=self.spec.id,
        )

    def _extract_caller_deadline(self, context: RequestContext) -> float | None:
        """Extract the caller's remaining deadline from inbound params metadata."""
        raw = self._inbound_metadata(context).get("deadline_remaining")
        if raw is None:
            return None
        try:
            return float(raw)
        except (ValueError, TypeError):
            return None

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id or str(uuid.uuid4())
        context_id = context.context_id or str(uuid.uuid4())
        updater = TaskUpdater(event_queue=event_queue, task_id=task_id, context_id=context_id)
        raw_user_input = (context.get_user_input() or "").strip()
        called_as_peer = is_peer_call(raw_user_input)
        user_input = strip_peer_marker(raw_user_input)

        exec_ctx = self._extract_exec_ctx(context)
        caller_deadline = self._extract_caller_deadline(context)
        with bind_context(exec_ctx):
            await self._execute_inner(
                context, updater, user_input, called_as_peer, task_id,
                caller_deadline=caller_deadline,
            )

    async def _execute_inner(
        self,
        context: RequestContext,
        updater: TaskUpdater,
        user_input: str,
        called_as_peer: bool,
        task_id: str,
        *,
        caller_deadline: float | None = None,
    ) -> None:
        # 1. announce submission for brand-new requests
        if context.current_task is None:
            await updater.submit(
                updater.new_agent_message(
                    parts=[_text_part(f"Accepted by `{self.spec.id}` agent.")],
                    metadata={
                        "phase": "submitted",
                        "agent": self.spec.id,
                        "called_as_peer": called_as_peer,
                    },
                )
            )

        if not user_input:
            await updater.requires_input(
                updater.new_agent_message(
                    parts=[_text_part("Please provide a non-empty question.")],
                    metadata={"phase": "clarification_required"},
                )
            )
            return

        # 2. move to working
        await updater.start_work(
            updater.new_agent_message(
                parts=[_text_part(f"{self.spec.id} agent is thinking…")],
                metadata={"phase": "working", "agent": self.spec.id},
            )
        )

        # 3. decide between simple-stream and tool-loop paths, under an
        #    overall wall-clock deadline — capped to the caller's remaining
        #    budget when this agent was invoked as a peer.
        eff_dl = effective_deadline(caller_deadline)
        consultations: list[tuple[str, str, str]] = []  # (peer_name, question, peer_text)
        try:
            async with deadline(eff_dl):
                if called_as_peer or not self.peers:
                    # Simple streaming path: no peer tool offered. When invoked
                    # as a peer, answer the sub-question tersely (brevity + cap).
                    final_text = await self._run_streaming(updater, user_input, concise=called_as_peer)
                else:
                    # Tool-loop path: peer consultation enabled uses A2A JSON-RPC to invoke peers.
                    final_text = await self._run_tool_loop(updater, user_input, consultations)
        except asyncio.CancelledError:
            logger.info("agent.cancelled agent=%s task_id=%s", self.spec.id, task_id)
            raise
        except DeadlineExceeded as exc:
            logger.warning("agent.deadline_exceeded agent=%s err=%s", self.spec.id, exc)
            await updater.failed(
                updater.new_agent_message(
                    parts=[_text_part(
                        f"Task exceeded the {DEFAULT_TASK_DEADLINE:.0f}s deadline "
                        f"and was cancelled to prevent runaway work."
                    )],
                    metadata={
                        "phase": "failed",
                        "reason": "deadline_exceeded",
                        "agent": self.spec.id,
                    },
                )
            )
            return
        except Exception as exc:
            logger.exception("agent.execute_failed agent=%s", self.spec.id)
            await updater.failed(
                updater.new_agent_message(
                    parts=[_text_part(f"Agent `{self.spec.id}` encountered an internal error.")],
                    metadata={"phase": "failed", "agent": self.spec.id,
                              "error_type": type(exc).__name__},
                )
            )
            return

        # 4. append a "Consulted peers" footer if any were used (top-level only).
        if consultations and not called_as_peer:
            footer = ["", "", "---", "**Consulted peers (A2A):**"]
            for peer_name, question, _ in consultations:
                deco = get_decoration(peer_name)
                footer.append(f"- {deco.icon} **{peer_name}** — _{question[:160]}_")
            final_text = final_text + "\n".join(footer)

        # 5. emit the final artifact + completed status.
        # LiteLLM's OpenAI-compat adapter reads the `complete()` message
        # text as the assistant content, not the artifact — so we put the
        # full answer in BOTH places.
        await updater.add_artifact(
            parts=[_text_part(final_text)],
            name="response",
            metadata={
                "agent": self.spec.id,
                "model_alias": self.spec.model_alias(),
                "peers_consulted": [c[0] for c in consultations],
            },
            last_chunk=True,
        )
        await updater.complete(
            updater.new_agent_message(
                parts=[_text_part(final_text)],
                metadata={"phase": "completed", "agent": self.spec.id},
            )
        )

    # ---- internal: streaming path (no peer tools) ------------------------

    async def _run_streaming(self, updater: TaskUpdater, user_input: str, *, concise: bool = False) -> str:
        """Direct streaming path, no A2A JSON-RPC involved.

        When ``concise`` (this agent was invoked as a peer), append the brevity
        directive and cap output so a consultation stays a focused sub-answer
        rather than a full deliverable.
        """
        system = self.spec.system_prompt
        max_tokens = None
        if concise:
            system = system + PEER_BREVITY_INSTRUCTION
            max_tokens = PEER_MAX_TOKENS
        collected: list[str] = []
        async for delta in stream_complete(
            model=self.spec.model_alias(),
            system=system,
            user=user_input,
            max_tokens=max_tokens,
        ):
            collected.append(delta)
            await updater.update_status(
                TaskState.working,
                updater.new_agent_message(
                    parts=[_text_part(delta)],
                    metadata={"phase": "progress", "agent": self.spec.id},
                ),
            )
        return "".join(collected)

    async def _stream_out(self, updater: TaskUpdater, text: str) -> str:
        """Replay an already-generated answer as progress deltas.

        The tool loop generates its final answer non-streamed; this emits it in
        small word-groups (phase="progress") so the orchestrator/UI render it
        progressively, without paying for a second (expensive) generation.
        """
        if not text:
            return text
        words = re.findall(r"\S+\s*", text)
        group: list[str] = []
        for w in words:
            group.append(w)
            if len(group) >= 6:
                await updater.update_status(
                    TaskState.working,
                    updater.new_agent_message(
                        parts=[_text_part("".join(group))],
                        metadata={"phase": "progress", "agent": self.spec.id},
                    ),
                )
                group = []
        if group:
            await updater.update_status(
                TaskState.working,
                updater.new_agent_message(
                    parts=[_text_part("".join(group))],
                    metadata={"phase": "progress", "agent": self.spec.id},
                ),
            )
        return text

    # ---- internal: tool-loop path (peer consultation enabled) -----------

    async def _run_tool_loop(
        self,
        updater: TaskUpdater,
        user_input: str,
        consultations: list[tuple[str, str, str]],
    ) -> str:
        """Run the peer-consultation tool loop for a top-level request.

        The agent calls the model with the `consult_peer` tool exposed
        (`tool_choice="auto"`). Each round either:
          - returns content with no tool calls → that's the final answer, OR
          - returns one or more `consult_peer` calls → we invoke each peer
            (depth-1: peers cannot consult further), feed the results back,
            and loop again.

        Two independent bounds:
          - DEPTH is capped at 1 by the peer-call marker (see peer_client):
            a consulted peer is served WITHOUT this tool, so it cannot
            recurse. This prevents A→B→C chains and A→B→A loops.
          - ROUNDS are capped at `_MAX_TOOL_ITERS`. If the model is still
            requesting tools when the cap is hit, we make ONE final call
            with `tool_choice="none"` to force a synthesized answer from
            the consultations already gathered (no placeholder bail-out).

        Worst case per top-level request: `_MAX_TOOL_ITERS` auto rounds + 1
        forced-synthesis round of LLM calls, plus the peer calls fired
        within those rounds (each peer = exactly 1 LLM call, 0 recursion).
        """
        tools = [_consult_peer_tool(self.peers)]
        system_prompt = self.spec.system_prompt + _peer_hint(self.peers)
        model_alias = self.spec.model_alias()
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input},
        ]

        for _iter in range(_MAX_TOOL_ITERS):
            async def _llm_call():
                client = make_async_client(timeout=remaining_or(DEFAULT_TASK_DEADLINE))
                return await client.chat.completions.create(
                    model=model_alias,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                )
            resp = await with_retry(_llm_call, target=model_alias)
            choice = resp.choices[0]
            msg = choice.message
            tool_calls = msg.tool_calls or []

            if not tool_calls:
                # Final answer. Stream it out as progress deltas so the
                # orchestrator (and UI) render it progressively, just like the
                # leaf path — the tool loop itself is non-streaming.
                return await self._stream_out(updater, (msg.content or "").strip())

            # Record the assistant turn that asked for tools.
            assistant_msg: dict = {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ],
            }
            messages.append(assistant_msg)

            # Execute each tool call (only consult_peer is supported).
            for tc in tool_calls:
                if tc.function.name != _PEER_TOOL_NAME:
                    tool_result = f"(unknown tool `{tc.function.name}`)"
                else:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    peer_name = str(args.get("peer_name", "")).strip()
                    question = str(args.get("question", "")).strip()

                    if peer_name not in self.peers or not question:
                        tool_result = (
                            f"(refused: peer `{peer_name}` not allowed; "
                            f"valid peers are {self.peers})"
                        )
                    else:
                        await updater.update_status(
                            TaskState.working,
                            updater.new_agent_message(
                                parts=[_text_part(
                                    f"📞 consulting peer `{peer_name}` — {question[:120]}"
                                )],
                                metadata={
                                    "phase": "consulting_peer",
                                    "agent": self.spec.id,
                                    "peer": peer_name,
                                },
                            ),
                        )
                        peer_text = await consult_peer(peer_name, question, caller=self.spec.id)
                        consultations.append((peer_name, question, peer_text))
                        tool_result = peer_text[:8000]  # cap context

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_result,
                })

        # Cap reached and the model is STILL asking for tools. Rather than
        # bail with a placeholder, force one final synthesis turn with
        # tool_choice="none" so the model must produce a real answer from
        # the consultations already gathered. (tools= is kept so the prior
        # tool_calls in history stay schema-valid; "none" forbids new ones.)
        logger.warning(
            "agent.tool_loop_capped agent=%s max_iters=%d",
            self.spec.id, _MAX_TOOL_ITERS,
        )
        messages.append({
            "role": "user",
            "content": (
                "You have gathered enough peer input. Do NOT request any more "
                "consultations. Write your COMPLETE final answer now, in your "
                "standard structured format, incorporating the peer responses above."
            ),
        })
        async def _synth_call():
            client = make_async_client(timeout=remaining_or(DEFAULT_TASK_DEADLINE))
            return await client.chat.completions.create(
                model=model_alias,
                messages=messages,
                tools=tools,
                tool_choice="none",
            )
        final_resp = await with_retry(_synth_call, target=model_alias)
        final_text = (final_resp.choices[0].message.content or "").strip()
        return await self._stream_out(
            updater,
            final_text or "(No final answer was produced after the peer consultations above.)",
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=context.task_id or "",
            context_id=context.context_id or "",
        )
        await updater.cancel(
            updater.new_agent_message(
                parts=[_text_part("Cancellation acknowledged.")],
                metadata={"phase": "cancelled", "agent": self.spec.id},
            )
        )


# ---------------------------------------------------------------------------
#  Starlette app factory
# ---------------------------------------------------------------------------


def _index_route_factory(spec: SpecialistSpec):
    deco = get_decoration(spec.id)

    async def index(_request: Request) -> JSONResponse:
        return JSONResponse({
            "agent_id": spec.id,
            "name": spec.id,
            "role": deco.role,
            "icon": deco.icon,
            "color": deco.color,
            "model_alias": spec.model_alias(),
            "endpoints": {
                "agent_card": "/.well-known/agent-card.json",
                "a2a_jsonrpc": "/a2a",
                "convenience_sse": "/invoke",
                "convenience_sync": "/invoke-sync",
                "health": "/healthz",
            },
        })
    return index


async def _healthz(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


def _convenience_routes_factory(spec: SpecialistSpec) -> list[Route]:
    """Non-A2A endpoints for curl/testing without forming JSON-RPC."""

    async def _generate_text(user_text: str) -> str:
        parts: list[str] = []
        async for delta in stream_complete(
            model=spec.model_alias(),
            system=spec.system_prompt,
            user=user_text,
        ):
            parts.append(delta)
        return "".join(parts)

    async def invoke_sync(request: Request):
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "expected JSON body"}, status_code=400)
        query = (payload or {}).get("query", "").strip()
        if not query:
            return JSONResponse({"error": "`query` must be non-empty"}, status_code=400)
        text = await _generate_text(query)
        return JSONResponse({
            "agent_id": spec.id,
            "model_alias": spec.model_alias(),
            "response": text,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        })

    async def invoke_sse(request: Request):
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "expected JSON body"}, status_code=400)
        query = (payload or {}).get("query", "").strip()
        session_id = (payload or {}).get("session_id")
        if not query:
            return JSONResponse({"error": "`query` must be non-empty"}, status_code=400)

        deco = get_decoration(spec.id)

        async def event_gen():
            yield {
                "event": "accepted",
                "data": json.dumps({
                    "agent_id": spec.id,
                    "icon": deco.icon,
                    "color": deco.color,
                    "role": deco.role,
                    "model_alias": spec.model_alias(),
                    "session_id": session_id,
                }),
            }
            collected: list[str] = []
            try:
                async for delta in stream_complete(
                    model=spec.model_alias(),
                    system=spec.system_prompt,
                    user=query,
                ):
                    collected.append(delta)
                    yield {
                        "event": "delta",
                        "data": json.dumps({"agent_id": spec.id, "text": delta}),
                    }
            except Exception as exc:
                yield {
                    "event": "error",
                    "data": json.dumps({"agent_id": spec.id, "error": str(exc)}),
                }
                return
            yield {
                "event": "final",
                "data": json.dumps({
                    "agent_id": spec.id,
                    "model_alias": spec.model_alias(),
                    "response": "".join(collected),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                }),
            }

        return EventSourceResponse(event_gen())

    return [
        Route("/invoke-sync", endpoint=invoke_sync, methods=["POST"]),
        Route("/invoke", endpoint=invoke_sse, methods=["POST"]),
    ]


def card_alias_routes(card) -> list[Route]:
    """Serve the agent card under /a2a/.well-known/* in addition to the root.

    a2a-sdk serves the card at the ROOT well-known path
    (`/.well-known/agent-card.json`). But the LiteLLM Agent Gateway fetches the
    card *relative to the registered RPC url* — which ends in `/a2a` — so it
    GETs `/a2a/.well-known/agent-card.json`. Without these aliases that fetch
    404s and native `message/send` (the jsonrpc transport) fails. RPC routing
    is unchanged; this only makes the card discoverable at both paths.
    """
    payload = card.model_dump(mode="json", by_alias=True, exclude_none=True)

    async def _card(_request: Request) -> JSONResponse:
        return JSONResponse(payload)

    return [
        Route("/a2a/.well-known/agent-card.json", endpoint=_card, methods=["GET"]),
        Route("/a2a/.well-known/agent.json", endpoint=_card, methods=["GET"]),
    ]


def build_app(spec: SpecialistSpec):
    card = spec.build_card()
    handler = DefaultRequestHandler(
        agent_executor=SpecialistAgentExecutor(spec),
        task_store=InMemoryTaskStore(),
    )
    a2a_app = A2AStarletteApplication(agent_card=card, http_handler=handler)
    # build() returns a Starlette app with the agent-card + JSON-RPC routes wired.
    app = a2a_app.build(rpc_url="/a2a")

    # Add our own routes alongside.
    extra_routes = [Route("/", endpoint=_index_route_factory(spec))]
    extra_routes.append(Route("/healthz", endpoint=_healthz))
    extra_routes.extend(card_alias_routes(card))
    extra_routes.extend(_convenience_routes_factory(spec))
    for route in extra_routes:
        app.router.routes.append(route)

    logger.info(
        "[%s] card: name=%s url=%s skills=%d",
        spec.id, card.name, card.url, len(card.skills),
    )
    return app


def run_specialist(agent_id: str) -> None:
    spec = get_spec(agent_id)
    port = spec.resolve_port()
    host = get_settings().agent_host
    app = build_app(spec)
    logger.info("agent.startup agent=%s host=%s port=%s", agent_id, host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")
