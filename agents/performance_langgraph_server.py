"""Performance agent — powered by LangGraph (cross-framework A2A).

This agent replaces the a2a-sdk-only path for `performance` with one built
on a LangGraph state machine. It proves a third framework interoperates:
the LangGraph agent exposes the *exact same* A2A endpoints
(/.well-known/agent-card.json, POST /a2a) as every other specialist, so
LiteLLM, the router, peer consultation, and the Streamlit UI see no
difference.

Internals (what makes it "LangGraph"):
  A compiled StateGraph with two sequential nodes sharing a typed state —
    START → hypotheses → recommendations → END
  Each node is an async function that calls the shared LiteLLM client
  (so the no-`temperature` handling and the single gateway are reused).
  The A2A executor drives the graph with `astream(..., stream_mode="updates")`
  and turns each node update into a `working` status event.

LLM calls route through the shared LiteLLM proxy .
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from operator import add
from pathlib import Path
from typing import Annotated, TypedDict

import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import Part, TaskState, TextPart
from langgraph.graph import END, START, StateGraph
from sse_starlette.sse import EventSourceResponse
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.specialists import get_spec  # noqa: E402
from config import get_settings  # noqa: E402
from common.deadline import DEFAULT_TASK_DEADLINE, DeadlineExceeded, deadline, effective_deadline  # noqa: E402
from common.decorations import get_decoration  # noqa: E402
from common.llm_client import complete, stream_complete  # noqa: E402
from common.peer_client import (  # noqa: E402
    PEER_BREVITY_INSTRUCTION,
    PEER_MAX_TOKENS,
    is_peer_call,
    strip_peer_marker,
)
from observability.context import ExecutionContext, bind_context  # noqa: E402

from common.log import setup as _setup_logging
_setup_logging()
logger = logging.getLogger("multi_agent.performance_langgraph")

_SPEC = get_spec("performance")


# ---------------------------------------------------------------------------
#  LangGraph state + nodes
# ---------------------------------------------------------------------------


class PerfState(TypedDict, total=False):
    """Shared state threaded through the performance reasoning graph."""

    request: str
    hypotheses: str        # "### Hypotheses" + "### Measurements to Run"
    recommendations: str   # "### Recommended Fixes" + "### Expected Impact"
    final_output: str
    activity_log: Annotated[list[str], add]


_HYPOTHESES_INSTRUCTION = """Focus ONLY on diagnosis for now. Given the user's
performance problem, output EXACTLY these two markdown sections and nothing else:

### Hypotheses
Ranked list of the most likely causes, each with a one-line rationale.

### Measurements to Run
The exact tools/metrics to confirm or kill each hypothesis (e.g. `EXPLAIN
ANALYZE`, `py-spy`, p95/p99, flame graphs)."""

_RECOMMENDATIONS_INSTRUCTION = """You have already produced hypotheses and the
measurements to confirm them (included below). Now output EXACTLY these two
markdown sections and nothing else:

### Recommended Fixes
Concrete code/config changes, ordered by ROI, each mapped to the hypothesis
it addresses.

### Expected Impact
Quantify the likely win (e.g. "should cut p99 from ~X to ~Y")."""


async def hypotheses_node(state: PerfState) -> PerfState:
    """Node 1 — diagnose: hypotheses + measurements."""
    user = f"Performance problem:\n{state['request']}\n\n{_HYPOTHESES_INSTRUCTION}"
    text = await complete(model=_SPEC.model_alias(), system=_SPEC.system_prompt, user=user)
    logger.info("graph.node_done node=hypotheses chars=%d", len(text))
    return {"hypotheses": text, "activity_log": ["hypotheses+measurements drafted"]}


async def recommendations_node(state: PerfState) -> PerfState:
    """Node 2 — prescribe: fixes + expected impact (sees node 1's output)."""
    user = (
        f"Performance problem:\n{state['request']}\n\n"
        f"Your diagnosis so far:\n{state.get('hypotheses', '')}\n\n"
        f"{_RECOMMENDATIONS_INSTRUCTION}"
    )
    text = await complete(model=_SPEC.model_alias(), system=_SPEC.system_prompt, user=user)
    logger.info("graph.node_done node=recommendations chars=%d", len(text))
    final = "\n\n".join([state.get("hypotheses", "").strip(), text.strip()]).strip()
    return {
        "recommendations": text,
        "final_output": final,
        "activity_log": ["fixes+impact drafted"],
    }


def build_graph():
    """Compile the performance reasoning state machine.

    Node names differ from state keys (LangGraph forbids collisions):
      START → diagnose → prescribe → END
    `diagnose` writes state['hypotheses']; `prescribe` writes
    state['recommendations'] + state['final_output'].
    """
    g = StateGraph(PerfState)
    g.add_node("diagnose", hypotheses_node)
    g.add_node("prescribe", recommendations_node)
    g.add_edge(START, "diagnose")
    g.add_edge("diagnose", "prescribe")
    g.add_edge("prescribe", END)
    return g.compile(name="performance-langgraph")


_GRAPH = build_graph()

_NODE_SUMMARY = {
    "diagnose": "diagnosed: hypotheses + measurements to run",
    "prescribe": "prescribed: fixes + expected impact",
}


# ---------------------------------------------------------------------------
#  A2A executor that drives the LangGraph graph
# ---------------------------------------------------------------------------


def _text_part(text: str) -> Part:
    return Part(root=TextPart(text=text))


class LangGraphPerformanceExecutor(AgentExecutor):
    """Drive the compiled StateGraph and emit A2A lifecycle events."""

    @staticmethod
    def _inbound_metadata(context: RequestContext) -> dict:
        """Extract JSON-RPC params.metadata from the inbound request.

        ``current_task`` is None on fresh requests — not a valid source.
        """
        for src in (
            getattr(context, "metadata", None),
            getattr(getattr(context, "message", None), "metadata", None),
        ):
            if isinstance(src, dict) and src:
                return src
        return {}

    def _extract_caller_deadline(self, context: RequestContext) -> float | None:
        """Extract the caller's remaining deadline from inbound metadata."""
        try:
            raw = self._inbound_metadata(context).get("deadline_remaining")
            if raw is not None:
                return float(raw)
        except Exception:
            pass
        return None

    def _extract_exec_ctx(self, context: RequestContext) -> ExecutionContext:
        """Recover propagated ExecutionContext or synthesize a root."""
        meta = self._inbound_metadata(context)
        ec_dict = meta.get("execution_context")
        if isinstance(ec_dict, dict):
            try:
                ctx = ExecutionContext(**ec_dict)
                logger.info(
                    "ctx.propagated agent=performance conversation=%s trace=%s span=%s",
                    ctx.conversation_id[:8], ctx.trace_id[:8], ctx.span_id[:8],
                )
                return ctx
            except Exception:
                logger.warning("ctx.parse_failed agent=performance", exc_info=True)
        return ExecutionContext.new_root(
            conversation_id=context.context_id or "",
            workflow_name="performance",
        )

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=context.task_id or "",
            context_id=context.context_id or "",
        )
        raw_input = (context.get_user_input() or "").strip()
        user_input = strip_peer_marker(raw_input)
        exec_ctx = self._extract_exec_ctx(context)

        with bind_context(exec_ctx):
            if context.current_task is None:
                await updater.submit(
                    updater.new_agent_message(
                        parts=[_text_part("Accepted by `performance` agent (LangGraph).")],
                        metadata={"phase": "submitted", "agent": "performance", "framework": "langgraph"},
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

            await updater.start_work(
                updater.new_agent_message(
                    parts=[_text_part("performance agent (LangGraph) is running its diagnosis graph…")],
                    metadata={"phase": "working", "agent": "performance", "framework": "langgraph"},
                )
            )

            caller_dl = self._extract_caller_deadline(context)
            eff_dl = effective_deadline(caller_dl)

            # Consulted as a peer → answer the focused sub-question concisely
            # and stream it. Skip the full diagnose→prescribe graph (that's for
            # top-level requests and would balloon the consult latency).
            if is_peer_call(raw_input):
                try:
                    async with deadline(eff_dl):
                        collected: list[str] = []
                        async for delta in stream_complete(
                            model=_SPEC.model_alias(),
                            system=_SPEC.system_prompt + PEER_BREVITY_INSTRUCTION,
                            user=user_input,
                            max_tokens=PEER_MAX_TOKENS,
                        ):
                            collected.append(delta)
                            await updater.update_status(
                                TaskState.working,
                                updater.new_agent_message(
                                    parts=[_text_part(delta)],
                                    metadata={"phase": "progress", "agent": "performance",
                                              "framework": "langgraph"},
                                ),
                            )
                    final_text = "".join(collected) or "(no output produced)"
                except DeadlineExceeded as exc:
                    logger.warning("agent.deadline_exceeded agent=performance (peer) err=%s", exc)
                    await updater.failed(updater.new_agent_message(
                        parts=[_text_part("Performance consult exceeded its deadline.")],
                        metadata={"phase": "failed", "reason": "deadline_exceeded", "agent": "performance"},
                    ))
                    return
                await updater.add_artifact(
                    parts=[_text_part(final_text)], name="response", last_chunk=True,
                    metadata={"agent": "performance", "framework": "langgraph"},
                )
                await updater.complete(updater.new_agent_message(
                    parts=[_text_part(final_text)],
                    metadata={"phase": "completed", "agent": "performance", "framework": "langgraph"},
                ))
                return

            state: PerfState = {"request": user_input, "activity_log": []}
            try:
                async with deadline(eff_dl):
                    async for update in _GRAPH.astream(state, stream_mode="updates"):
                        node_name, delta = next(iter(update.items()))
                        if isinstance(delta, dict):
                            state.update({k: v for k, v in delta.items() if k != "activity_log"})
                        summary = _NODE_SUMMARY.get(node_name, f"{node_name} step complete")
                        await updater.update_status(
                            TaskState.working,
                            updater.new_agent_message(
                                parts=[_text_part(f"[{node_name}] {summary}")],
                                metadata={"phase": "progress", "agent": "performance",
                                          "framework": "langgraph", "node": node_name},
                            ),
                        )
            except DeadlineExceeded as exc:
                logger.warning("agent.deadline_exceeded agent=performance err=%s", exc)
                await updater.failed(
                    updater.new_agent_message(
                        parts=[_text_part(
                            f"Performance graph exceeded the {DEFAULT_TASK_DEADLINE:.0f}s "
                            f"deadline and was cancelled."
                        )],
                        metadata={
                            "phase": "failed",
                            "reason": "deadline_exceeded",
                            "agent": "performance",
                            "framework": "langgraph",
                        },
                    )
                )
                return
            except Exception as exc:
                logger.exception("agent.execute_failed agent=performance")
                await updater.failed(
                    updater.new_agent_message(
                        parts=[_text_part(f"Performance graph failed: {exc}")],
                        metadata={"phase": "failed", "agent": "performance"},
                    )
                )
                return

            final_text = state.get("final_output") or "(no output produced)"
            await updater.add_artifact(
                parts=[_text_part(final_text)],
                name="response",
                metadata={"agent": "performance", "framework": "langgraph",
                          "model_alias": _SPEC.model_alias()},
                last_chunk=True,
            )
            await updater.complete(
                updater.new_agent_message(
                    parts=[_text_part(final_text)],
                    metadata={"phase": "completed", "agent": "performance", "framework": "langgraph"},
                )
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
                metadata={"phase": "cancelled", "agent": "performance"},
            )
        )


# ---------------------------------------------------------------------------
#  Starlette app factory — same A2A surface as every other specialist
# ---------------------------------------------------------------------------


def build_app():
    spec = _SPEC
    card = spec.build_card()
    handler = DefaultRequestHandler(
        agent_executor=LangGraphPerformanceExecutor(),
        task_store=InMemoryTaskStore(),
    )
    app = A2AStarletteApplication(agent_card=card, http_handler=handler).build(rpc_url="/a2a")

    deco = get_decoration(spec.id)

    async def index(_request: Request) -> JSONResponse:
        return JSONResponse({
            "agent_id": spec.id,
            "name": spec.id,
            "framework": "langgraph",
            "role": deco.role,
            "icon": deco.icon,
            "color": deco.color,
            "model_alias": spec.model_alias(),
            "graph_nodes": ["diagnose", "prescribe"],
            "endpoints": {
                "agent_card": "/.well-known/agent-card.json",
                "a2a_jsonrpc": "/a2a",
                "convenience_sse": "/invoke",
                "convenience_sync": "/invoke-sync",
                "health": "/healthz",
            },
        })

    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def invoke_sync(request: Request):
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "expected JSON body"}, status_code=400)
        query = (payload or {}).get("query", "").strip()
        if not query:
            return JSONResponse({"error": "`query` must be non-empty"}, status_code=400)
        result = await _GRAPH.ainvoke({"request": query, "activity_log": []})
        return JSONResponse({
            "agent_id": spec.id,
            "framework": "langgraph",
            "model_alias": spec.model_alias(),
            "response": result.get("final_output", ""),
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

        async def event_gen():
            yield {
                "event": "accepted",
                "data": json.dumps({
                    "agent_id": spec.id, "framework": "langgraph",
                    "icon": deco.icon, "color": deco.color, "role": deco.role,
                    "model_alias": spec.model_alias(), "session_id": session_id,
                }),
            }
            state: PerfState = {"request": query, "activity_log": []}
            try:
                async for update in _GRAPH.astream(state, stream_mode="updates"):
                    node_name, delta = next(iter(update.items()))
                    if isinstance(delta, dict):
                        state.update({k: v for k, v in delta.items() if k != "activity_log"})
                    yield {
                        "event": "delta",
                        "data": json.dumps({
                            "agent_id": spec.id, "node": node_name,
                            "text": f"\n\n[{node_name}] {_NODE_SUMMARY.get(node_name, '')}\n",
                        }),
                    }
            except Exception as exc:
                yield {"event": "error", "data": json.dumps({"agent_id": spec.id, "error": str(exc)})}
                return
            yield {
                "event": "final",
                "data": json.dumps({
                    "agent_id": spec.id, "framework": "langgraph",
                    "model_alias": spec.model_alias(),
                    "response": state.get("final_output", ""),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                }),
            }

        return EventSourceResponse(event_gen())

    from agents.base_server import card_alias_routes
    for route in [
        Route("/", endpoint=index),
        Route("/healthz", endpoint=healthz),
        Route("/invoke-sync", endpoint=invoke_sync, methods=["POST"]),
        Route("/invoke", endpoint=invoke_sse, methods=["POST"]),
        *card_alias_routes(card),   # /a2a/.well-known/* for LiteLLM's jsonrpc fetch
    ]:
        app.router.routes.append(route)

    logger.info(
        "[performance] LangGraph agent ready: model=%s url=%s nodes=hypotheses→recommendations",
        spec.model_alias(), card.url,
    )
    return app


def run_performance_langgraph() -> None:
    port = _SPEC.resolve_port()
    host = get_settings().agent_host
    app = build_app()
    logger.info("agent.startup agent=performance framework=langgraph host=%s port=%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_performance_langgraph()
