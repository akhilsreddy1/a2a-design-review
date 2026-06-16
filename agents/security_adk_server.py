"""Security agent — powered by Google ADK (cross-framework A2A).

Proving a third framework interoperates: this agent is built on Google ADK instead of the a2a-sdk, but still talks to the same peers and is orchestrated by the same a2a-core. 
The ADK agent's instruction is augmented with a roster of allowed peers and guidelines for when/how to consult them, and the `consult_peer` function is implemented as a first-class ADK tool that routes through the LiteLLM Agent Gateway — giving the ADK agent the same depth-1 peer consultation ability as the native a2a-sdk agents.

LLM calls route through the shared LiteLLM proxy .

"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from uuid import uuid4
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import Part, TaskState, TextPart
from sse_starlette.sse import EventSourceResponse
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.specialists import get_spec  # noqa: E402
from config import get_settings  # noqa: E402
from common.decorations import get_decoration  # noqa: E402
from common.env_utils import (  # noqa: E402
    get_litellm_base_url,
    get_litellm_key,
)
from common.llm_client import stream_complete  # noqa: E402
from common.deadline import DEFAULT_TASK_DEADLINE, DeadlineExceeded, deadline, effective_deadline  # noqa: E402
from common.peer_client import (  # noqa: E402
    consult_peer as _consult_peer_via_gateway,
    is_peer_call,
    strip_peer_marker,
    PEER_BREVITY_INSTRUCTION,
)
from observability.context import ExecutionContext, bind_context  # noqa: E402

# --- Google ADK imports ---
import litellm as _litellm  # noqa: E402
from google.adk.agents import LlmAgent  # noqa: E402
from google.adk.agents.run_config import RunConfig, StreamingMode  # noqa: E402
from google.adk.a2a.executor.a2a_agent_executor import A2aAgentExecutor  # noqa: E402
from google.genai import types as genai_types  # noqa: E402
from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService  # noqa: E402
from google.adk.auth.credential_service.in_memory_credential_service import InMemoryCredentialService  # noqa: E402
from google.adk.memory.in_memory_memory_service import InMemoryMemoryService  # noqa: E402
from google.adk.models.lite_llm import LiteLlm  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions.in_memory_session_service import InMemorySessionService  # noqa: E402

from common.log import setup as _setup_logging
_setup_logging()
logger = logging.getLogger("multi_agent.security_adk")


# ---------------------------------------------------------------------------
#  Peer consultation as an ADK function tool
#
#  This gives the ADK agent the SAME outbound peer-consultation ability the
#  native a2a-sdk agents have — completing the matrix (ADK → a2a-sdk). The
#  loop guard is unchanged: `consult_peer` (in common/peer_client) embeds the
#  depth-1 marker, so whichever peer we call answers WITHOUT its own peer tool.
# ---------------------------------------------------------------------------

# Which agents this agent may consult (from the shared decorations).
_PEERS: list[str] = list(get_decoration("security").peers)

# Per-request cap on how many times this agent may call `consult_peer`.
# The native a2a-sdk path bounds rounds with `_MAX_TOOL_ITERS`; the ADK
# Runner drives its own internal tool loop, so we instead cap the actual
# (expensive) peer calls. Matches base_server's default of 4.
_MAX_PEER_CONSULTS: int = int(os.getenv("A2A_MAX_PEER_CONSULTS", "4"))

# Set per-request when THIS agent is itself being consulted by a peer.
# While set, `consult_peer` refuses — enforcing the same depth-1 cap the
# native a2a-sdk agents get by dropping their tool. contextvars are
# asyncio-safe, so concurrent requests don't bleed into each other.
import contextvars  # noqa: E402

_IN_PEER_CALL: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "security_in_peer_call", default=False
)

# Per-request consultation budget. We store a MUTABLE [count] list (not a
# bare int) so the counter is shared even if ADK runs tool calls in child
# tasks — a child task copies the contextvar *value* (the same list object),
# so increments are visible across calls within one request.
_CONSULT_BUDGET: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar(
    "security_consult_budget", default=None
)


async def consult_peer(peer_name: str, question: str) -> dict:
    """Consult another specialist agent over A2A and return their answer.

    Use this ONLY when the question genuinely needs another specialist's
    lens and you cannot answer it confidently on your own. Frame a focused,
    self-contained sub-question — the peer does not see this conversation.
    The list of agents you are allowed to consult is given in your
    instructions; consulting anyone else will be refused.

    Args:
        peer_name: The specialist agent to consult (e.g. "developer").
        question: A self-contained question for that peer.

    Returns:
        A dict: {"status": "ok", "peer", "answer"} on success, or
        {"status": "refused", "reason"} if the peer is not allowed, this
        request is itself a peer call (depth-1 cap), or the per-request
        consultation budget is exhausted (iteration cap).
    """
    if _IN_PEER_CALL.get():
        return {"status": "refused",
                "reason": "this request is itself a peer consultation; "
                          "cannot consult further (depth-1 cap)"}
    if peer_name not in _PEERS:
        return {"status": "refused",
                "reason": f"peer '{peer_name}' not allowed; valid peers: {_PEERS}"}

    # Iteration cap: bound the number of peer calls per top-level request.
    budget = _CONSULT_BUDGET.get()
    if budget is not None:
        if budget[0] >= _MAX_PEER_CONSULTS:
            logger.warning(
                "[security/adk] consult budget exhausted (%d); refusing further peers",
                _MAX_PEER_CONSULTS,
            )
            return {"status": "refused",
                    "reason": f"consultation budget exhausted ({_MAX_PEER_CONSULTS} max). "
                              "Do NOT consult again — answer now from what you have."}
        budget[0] += 1

    logger.info("peer.consult.start peer=%s call=%s/%s question_chars=%d",
                peer_name, (budget[0] if budget else "?"), _MAX_PEER_CONSULTS, len(question))
    answer = await _consult_peer_via_gateway(peer_name, question, caller="security")
    return {"status": "ok", "peer": peer_name, "answer": answer}


def _part(text: str) -> Part:
    return Part(root=TextPart(text=text))


class StreamingAdkExecutor(AgentExecutor):
    """Drives the ADK Runner in SSE streaming mode and emits A2A deltas.

    Instead of ADK's built-in ``A2aAgentExecutor`` (which buffers the whole
    answer and emits it as one artifact), we run ``runner.run_async`` with
    ``StreamingMode.SSE`` ourselves and translate each *partial* ADK event
    into an A2A ``update_status`` carrying ``phase="progress"`` — the exact
    convention the native a2a-sdk agents use, so the orchestrator streams
    ADK answers token-by-token just like the rest.

    Cross-cutting concerns preserved from the previous wrapper:
    - **Depth-1 peer guard** (``_IN_PEER_CALL`` contextvar) + **consult
      budget** (``_CONSULT_BUDGET``).
    - **Propagated ExecutionContext** (so nested peer spans correlate).
    - **Wall-clock deadline**, capped to the caller's remaining budget.
    """

    def __init__(self, runner: Runner, *, agent_id: str = "security") -> None:
        self.runner = runner
        self.agent_id = agent_id

    @staticmethod
    def _inbound_metadata(context) -> dict:
        """Extract the JSON-RPC params.metadata from the inbound request.

        Checks ``context.metadata`` first (MessageSendParams-level), then
        falls back to message-level metadata. ``current_task`` is None on
        fresh requests, so it is NOT a valid source.
        """
        for src in (
            getattr(context, "metadata", None),
            getattr(getattr(context, "message", None), "metadata", None),
        ):
            if isinstance(src, dict) and src:
                return src
        return {}

    def _extract_caller_deadline(self, context) -> float | None:
        """Extract the caller's remaining deadline from inbound metadata."""
        try:
            raw = self._inbound_metadata(context).get("deadline_remaining")
            if raw is not None:
                return float(raw)
        except Exception:
            pass
        return None

    def _extract_exec_ctx(self, context) -> ExecutionContext:
        """Recover propagated ExecutionContext or synthesize a root."""
        meta = self._inbound_metadata(context)
        ec_dict = meta.get("execution_context")
        if isinstance(ec_dict, dict):
            try:
                ctx = ExecutionContext(**ec_dict)
                logger.info(
                    "ctx.propagated agent=security conversation=%s trace=%s span=%s",
                    ctx.conversation_id[:8], ctx.trace_id[:8], ctx.span_id[:8],
                )
                return ctx
            except Exception:
                logger.warning("ctx.parse_failed agent=security", exc_info=True)
        return ExecutionContext.new_root(
            conversation_id=context.context_id or "",
            workflow_name="security",
        )

    @staticmethod
    def _strip_marker_from_context(context) -> None:
        """Remove the peer-call marker from user message parts so ADK's
        runner doesn't see it. Mutates the context's message in place."""
        from common.peer_client import PEER_CALL_MARKER
        try:
            msg = getattr(context, "message", None)
            if msg and hasattr(msg, "parts"):
                for part in msg.parts:
                    tp = getattr(part, "root", part)
                    if hasattr(tp, "text") and PEER_CALL_MARKER in (tp.text or ""):
                        tp.text = tp.text.replace(PEER_CALL_MARKER, "").strip()
        except Exception:
            pass

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:  # type: ignore[override]
        task_id = context.task_id or str(uuid4())
        context_id = context.context_id or str(uuid4())
        updater = TaskUpdater(event_queue=event_queue, task_id=task_id, context_id=context_id)

        raw = context.get_user_input() or ""
        called_as_peer = is_peer_call(raw)
        user_text = strip_peer_marker(raw)

        token = _IN_PEER_CALL.set(called_as_peer)
        budget_token = _CONSULT_BUDGET.set([0])
        exec_ctx = self._extract_exec_ctx(context)
        caller_dl = self._extract_caller_deadline(context)
        eff_dl = effective_deadline(caller_dl)
        try:
            with bind_context(exec_ctx):
                if context.current_task is None:
                    await updater.submit(updater.new_agent_message(
                        parts=[_part("Accepted by `security` agent (Google ADK).")],
                        metadata={"phase": "submitted", "agent": self.agent_id,
                                  "called_as_peer": called_as_peer, "framework": "google-adk"},
                    ))
                if not user_text:
                    await updater.requires_input(updater.new_agent_message(
                        parts=[_part("Please provide a non-empty question.")],
                        metadata={"phase": "clarification_required"},
                    ))
                    return
                await updater.start_work(updater.new_agent_message(
                    parts=[_part("security agent (ADK) is thinking…")],
                    metadata={"phase": "working", "agent": self.agent_id, "framework": "google-adk"},
                ))
                async with deadline(eff_dl):
                    full = await self._stream_run(updater, user_text, concise=called_as_peer)
                await updater.add_artifact(
                    parts=[_part(full)], name="response", last_chunk=True,
                    metadata={"agent": self.agent_id, "framework": "google-adk"},
                )
                await updater.complete(updater.new_agent_message(
                    parts=[_part(full)],
                    metadata={"phase": "completed", "agent": self.agent_id, "framework": "google-adk"},
                ))
        except DeadlineExceeded as exc:
            logger.warning("agent.deadline_exceeded agent=security err=%s", exc)
            await updater.failed(updater.new_agent_message(
                parts=[_part(f"Security agent exceeded the {DEFAULT_TASK_DEADLINE:.0f}s deadline and was cancelled.")],
                metadata={"phase": "failed", "reason": "deadline_exceeded",
                          "agent": "security", "framework": "google-adk"},
            ))
        except Exception as exc:
            logger.exception("agent.execution_failed agent=security")
            await updater.failed(updater.new_agent_message(
                parts=[_part(f"Security agent failed: {exc}")],
                metadata={"phase": "failed", "agent": "security", "framework": "google-adk"},
            ))
        finally:
            _IN_PEER_CALL.reset(token)
            _CONSULT_BUDGET.reset(budget_token)

    async def _stream_run(self, updater: TaskUpdater, user_text: str, *, concise: bool = False) -> str:
        """Run the ADK Runner in SSE mode, emitting each partial as a delta.

        ADK partials are incremental; concatenating them yields the answer.
        The single non-partial final event repeats the whole answer, so we
        skip it (and only use it as a fallback if no partials streamed).

        When ``concise`` (invoked as a peer), the brevity directive is
        prepended to the user message — the ADK agent's instruction is fixed
        at creation time, so per-request guidance rides on the user turn.
        """
        if concise:
            user_text = f"{PEER_BREVITY_INSTRUCTION.strip()}\n\nQUESTION:\n{user_text}"
        session = await self.runner.session_service.create_session(
            app_name=self.agent_id, user_id="a2a",
        )
        message = genai_types.Content(role="user", parts=[genai_types.Part(text=user_text)])

        collected: list[str] = []
        final_full = ""
        async for event in self.runner.run_async(
            user_id="a2a",
            session_id=session.id,
            new_message=message,
            run_config=RunConfig(streaming_mode=StreamingMode.SSE),
        ):
            content = getattr(event, "content", None)
            if not content or not getattr(content, "parts", None):
                continue
            delta = "".join(p.text for p in content.parts if getattr(p, "text", None))
            if not delta:
                continue  # tool-call / function-response events carry no text
            if getattr(event, "partial", False):
                collected.append(delta)
                # Emit each partial as a progress update, so the orchestrator can stream it.
                await updater.update_status(
                    TaskState.working,
                    updater.new_agent_message(
                        parts=[_part(delta)],
                        metadata={"phase": "progress", "agent": self.agent_id},
                    ),
                )
            else:
                final_full = delta  # consolidated final (duplicate of the partials)

        return "".join(collected) or final_full

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:  # type: ignore[override]
        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=context.task_id or "",
            context_id=context.context_id or "",
        )
        await updater.cancel(updater.new_agent_message(
            parts=[_part("Cancellation acknowledged.")],
            metadata={"phase": "cancelled", "agent": "security"},
        ))



def _peer_instruction(peers: list[str]) -> str:
    """Roster hint appended to the ADK agent's instruction."""
    if not peers:
        return ""
    roster = "\n".join(
        f"- **{p}** ({get_decoration(p).role})" for p in peers
    )
    return (
        "\n\n---\n## Peer consultation (A2A)\n"
        "You can consult these peer specialists with the `consult_peer` tool:\n"
        f"{roster}\n\n"
        "Guidelines:\n"
        "- Only consult when the peer's lens materially improves your answer.\n"
        "- Send a focused, self-contained sub-question.\n"
        "- Weave their answer into YOUR structured response — don't just paste it.\n"
        "- When you do consult someone, end your answer with a "
        "`**Consulted peers (A2A):**` line naming them."
    )


# ---------------------------------------------------------------------------
#  ADK agent + runner factory
# ---------------------------------------------------------------------------

def _configure_litellm_proxy() -> None:
    """Point ADK's LiteLlm model at the shared LiteLLM proxy."""
    _litellm.use_litellm_proxy = True
    os.environ.setdefault("LITELLM_PROXY_API_KEY", get_litellm_key())
    os.environ.setdefault("LITELLM_PROXY_API_BASE", get_litellm_base_url())


def _make_runner(spec) -> Runner:
    _configure_litellm_proxy()

    adk_agent = LlmAgent(
        model=LiteLlm(model=spec.model_alias()),
        name=spec.id,
        description=spec.description,
        instruction=spec.system_prompt + _peer_instruction(_PEERS),
        tools=[consult_peer],          # ← ADK auto-wraps this as a FunctionTool
    )

    return Runner(
        app_name=spec.id,
        agent=adk_agent,
        artifact_service=InMemoryArtifactService(),
        session_service=InMemorySessionService(),
        memory_service=InMemoryMemoryService(),
        credential_service=InMemoryCredentialService(),
    )


# ---------------------------------------------------------------------------
#  Starlette app factory — mirrors base_server.build_app() but uses the
#  ADK A2aAgentExecutor instead of our SpecialistAgentExecutor.
# ---------------------------------------------------------------------------

def build_app(spec):
    runner = _make_runner(spec)
    executor = StreamingAdkExecutor(runner=runner, agent_id=spec.id)

    card = spec.build_card()
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
    )

    a2a_app = A2AStarletteApplication(agent_card=card, http_handler=handler)
    app = a2a_app.build(rpc_url="/a2a")

    # --- extra routes (same surface as the a2a-sdk specialists) -----------

    deco = get_decoration(spec.id)

    async def index(_request: Request) -> JSONResponse:
        return JSONResponse({
            "agent_id": spec.id,
            "name": spec.id,
            "framework": "google-adk",
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
        parts: list[str] = []
        async for delta in stream_complete(
            model=spec.model_alias(), system=spec.system_prompt, user=query,
        ):
            parts.append(delta)
        return JSONResponse({
            "agent_id": spec.id,
            "framework": "google-adk",
            "model_alias": spec.model_alias(),
            "response": "".join(parts),
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
                    "agent_id": spec.id,
                    "framework": "google-adk",
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
                    model=spec.model_alias(), system=spec.system_prompt, user=query,
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
                    "framework": "google-adk",
                    "model_alias": spec.model_alias(),
                    "response": "".join(collected),
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
        "[%s] ADK agent ready: model=%s url=%s skills=%d",
        spec.id, spec.model_alias(), card.url, len(card.skills),
    )
    return app


# ---------------------------------------------------------------------------
#  Entry point
# ---------------------------------------------------------------------------

def run_security_adk() -> None:
    spec = get_spec("security")
    port = spec.resolve_port()
    host = get_settings().agent_host
    app = build_app(spec)
    logger.info("agent.startup agent=security framework=adk host=%s port=%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_security_adk()
