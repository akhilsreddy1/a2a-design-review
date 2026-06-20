"""Event-driven SSE bridge — keeps orchestration and UI streaming decoupled.

    UI ──POST─►  POST /api/run                         (starts a run → 202)
    UI ──SSE──►  GET  /api/stream/{conversation_id}   (subscribes to the bus)
    Agents ───►  POST /api/events                      (nested spans → bus)
                       │
                run_orchestration()  emits typed events ──► event_bus
                       │                                       │
                       │                          ┌────────────┼──────────┐
                       │                        SSE          JSONL      console
                       ▼
            RouterOrchestrator → A2A agents (separate containers)

The bridge owns no agent logic. It builds an ExecutionContext, spawns the
orchestration as a task, and returns immediately; every observable step
arrives on the SSE stream. Multi-user isolation is by ``conversation_id``
on each subscription — one global bus, conversation-scoped streams.

The orchestrator emits events in-process via ``emit_*`` on the shared event bus. Nested
agent spans (peer consults, handoffs) report back via ``POST /api/events``. Published
events flow to all subscribers (SSE, JSONL, console).


Run:  uvicorn api.server:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from starlette.staticfiles import StaticFiles

from observability.context import ExecutionContext
from observability.event_bus import event_bus
from observability.events import parse_event
from observability.subscribers.console import register as register_console
from observability.subscribers.jsonl import JsonlTraceWriter
from observability.subscribers.sse import SSEConnection
from registry import LiteLLMRegistry
from router.orchestrator import RouterOrchestrator
from router.debate import DebateOrchestrator

from common.log import setup as _setup_logging
_setup_logging()
logger = logging.getLogger("multi_agent.bridge")

ORCHESTRATOR_ID = "orchestrator"
ORCHESTRATOR_COLOR = "#7F77DD"

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # disable proxy buffering so events flush live
}


async def agents_endpoint(_request: Request) -> JSONResponse:
    """
    Returns a list of all registered agents.
    """
    try:
        cards = await LiteLLMRegistry().discover_cards()
    except Exception as exc:
        logger.exception("agents.discovery_failed")
        return JSONResponse({"error": str(exc)}, status_code=502)

    roster = [{
        "id": ORCHESTRATOR_ID,
        "label": "Orchestrator",
        "role": "Route & coordinate",
        "color": ORCHESTRATOR_COLOR,
        "icon": "🛰️",
        "framework": None,
    }]
    for card in cards:
        meta = card.metadata
        roster.append({
            "id": card.name,
            "label": card.name.replace("_", " ").title(),
            "role": card.role or "Specialist agent",
            "color": meta.get("color", "#64748b"),
            "icon": meta.get("icon", "🤖"),
            "framework": meta.get("framework"),
            "model": meta.get("model") or meta.get("model_alias"),
        })
    return JSONResponse(roster)


# ── Run a conversation turn (emits events) ───────────────────────────────────
async def run_orchestration(ctx: ExecutionContext, query: str, pinned_agent: str | None) -> None:
    """
    Runs the RouterOrchestrator for one conversation turn, emitting events as it goes.
    """
    from common.deadline import DeadlineExceeded, deadline
    from observability.emitters import emit_agent_failed, emit_workflow_completed
    try:
        async with deadline():
            await RouterOrchestrator().run(ctx, query, pinned_agent=pinned_agent)
    except DeadlineExceeded:
        logger.warning("run.deadline_exceeded trace_id=%s", ctx.trace_id)
        await emit_agent_failed(ctx, ORCHESTRATOR_ID, error="overall deadline exceeded")
        await emit_workflow_completed(ctx, summary="Deadline exceeded")
    except Exception as exc:
        logger.exception("run.unhandled trace_id=%s", ctx.trace_id)
        await emit_agent_failed(ctx, ORCHESTRATOR_ID, error=str(exc)[:500])
        await emit_workflow_completed(ctx, summary=f"Unhandled error: {exc}"[:400])


async def run_debate(ctx: ExecutionContext, design: str, turns: int) -> None:
    """Run a structured design-review debate (hybrid orchestrator). The overall
    budget is sized for many reviewer/judge calls; each call self-limits."""
    from common.deadline import DeadlineExceeded, deadline
    from observability.emitters import emit_agent_failed, emit_workflow_completed
    try:
        async with deadline(max(300, turns * 200)):
            await DebateOrchestrator().run(ctx, design, max_turns=turns)
    except DeadlineExceeded:
        logger.warning("debate.deadline_exceeded trace_id=%s", ctx.trace_id)
        await emit_agent_failed(ctx, ORCHESTRATOR_ID, error="debate deadline exceeded")
        await emit_workflow_completed(ctx, summary="Debate deadline exceeded")
    except Exception as exc:
        logger.exception("debate.unhandled trace_id=%s", ctx.trace_id)
        await emit_agent_failed(ctx, ORCHESTRATOR_ID, error=str(exc)[:500])
        await emit_workflow_completed(ctx, summary=f"Unhandled error: {exc}"[:400])


async def run_endpoint(request: Request) -> JSONResponse:
    """Start a run. Body: {query, conversation_id?, session_id?, user_id?,
    tenant_id?, pinned_agent?}. Returns 202 with the correlation ids; events
    arrive on GET /api/stream/{conversation_id}."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    query = (body.get("query") or request.query_params.get("q") or "").strip()
    if not query:
        return JSONResponse({"error": "Missing 'query'."}, status_code=400)

    mode = (body.get("mode") or "route").strip().lower()
    ctx = ExecutionContext.new_root(
        conversation_id=body.get("conversation_id"),
        session_id=body.get("session_id"),
        user_id=body.get("user_id"),
        tenant_id=body.get("tenant_id"),
        workflow_name="debate" if mode == "debate" else "router",
    )
    if mode == "debate":
        asyncio.create_task(run_debate(ctx, query, int(body.get("turns") or 6)))
    else:
        pinned_agent = body.get("pinned_agent") or None
        asyncio.create_task(run_orchestration(ctx, query, pinned_agent))
    return JSONResponse(
        {
            "conversation_id": ctx.conversation_id,
            "session_id": ctx.session_id,
            "trace_id": ctx.trace_id,
            "span_id": ctx.span_id,
        },
        status_code=202,
    )


async def stream_endpoint(request: Request) -> StreamingResponse:
    """SSE stream of typed events for one conversation (multi-user isolation)."""
    conversation_id = request.path_params["conversation_id"]
    connection = SSEConnection(conversation_id)  # eager subscribe — no startup race

    async def generator():
        yield b": connected\n\n"
        async for event in connection.events():
            yield f"data: {json.dumps(event.to_json())}\n\n".encode("utf-8")

    return StreamingResponse(generator(), media_type="text/event-stream", headers=SSE_HEADERS)


async def healthz(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "service": "a2a-bridge"})


# ── Ingest: agents POST nested events back to the bridge ─────────────────────

async def events_ingest(request: Request) -> JSONResponse:
    """Accept a typed event from an agent process and republish on the bus.

    This is the HTTP replacement for FlowLogSource. Agents call
    ``observability.reporter.report_event(event_dict)`` when they do
    something worth observing (peer handoff, tool call, etc.). The bridge
    validates the payload, rebuilds a typed event, and publishes it onto
    the in-memory bus — so SSE, JSONL, and console subscribers all see
    nested spans without needing a shared filesystem.

    Agents must include at minimum: ``event_type``, ``session_id``,
    ``conversation_id``, ``trace_id``, ``span_id``.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    if not isinstance(body, dict) or "event_type" not in body:
        return JSONResponse({"error": "missing event_type"}, status_code=400)

    required = {"session_id", "conversation_id", "trace_id", "span_id"}
    missing = required - set(body)
    if missing:
        return JSONResponse({"error": f"missing fields: {missing}"}, status_code=400)

    try:
        event = parse_event(body)
    except Exception as exc:
        logger.warning("events.parse_failed err=%s", exc)
        return JSONResponse({"error": f"parse failed: {exc}"}, status_code=422)

    await event_bus.publish(event)
    logger.debug("events.ingested type=%s trace=%s agent=%s", event.event_type, event.trace_id[:8], event.agent_name)
    return JSONResponse({"ok": True, "event_id": event.event_id}, status_code=202)


# ── App wiring ───────────────────────────────────────────────────────────────
_subscriptions: list = []


def _register_subscribers() -> None:
    """Attach durable/observability sinks to the bus once at startup."""
    if _subscriptions:
        return
    _subscriptions.append(JsonlTraceWriter().register())
    _subscriptions.append(register_console())
    logger.info("observability.subscribers_registered count=%s", len(_subscriptions))


@asynccontextmanager
async def lifespan(_app: Starlette):
    _register_subscribers()
    yield


routes = [
    Route("/healthz", healthz, methods=["GET"]),
    Route("/api/agents", agents_endpoint, methods=["GET"]),
    Route("/api/run", run_endpoint, methods=["POST", "GET"]),
    Route("/api/stream/{conversation_id}", stream_endpoint, methods=["GET"]),
    Route("/api/events", events_ingest, methods=["POST"]),
]

middleware = [
    Middleware(
        CORSMiddleware,
        allow_origins=["*"],  # local dev tool
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )
]

app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan)

# Serve the built React UI (frontend/dist) at the root, so the bridge is a
# single process serving both the API and the app. The SPA's /api calls are
# same-origin. In dev, run `npm run dev` (Vite) which proxies /api here instead.
_DIST = PROJECT_ROOT / "frontend" / "dist"
if _DIST.exists():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="web")
    logger.info("ui.mounted dist=%s", _DIST)
else:
    logger.info("ui.not_built dist=%s (run `npm --prefix frontend run build`)", _DIST)
