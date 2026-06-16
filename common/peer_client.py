"""Peer-to-peer A2A consultation — one agent invokes another as a *client*.

A specialist's executor calls `consult_peer(peer_name, question)` when it
wants another specialist's expertise mid-task. The call goes through the
structured :class:`clients.a2a_client.A2AClient` — the SAME client the
orchestrator uses to invoke agents — NOT a raw chat-completion. The A2A
boundary is the agent-invocation API: "invoke this agent with this task"
returns a typed result, regardless of transport (native JSON-RPC by
default, or the OpenAI-compat route).

Nested observability
--------------------
If an :class:`observability.context.ExecutionContext` is bound (via
contextvar) when ``consult_peer`` runs, the function creates a child span
and POSTs typed handoff / started / completed events to the bridge via
:mod:`observability.reporter`. This is how nested peer calls appear in
the UI flow trail and JSONL traces, even though agents run in separate
containers.

Resilience
----------
`A2AClient.send` wraps each call in `with_retry`, so peer consultations
inherit the same exponential backoff + per-target circuit breaker as
every other call. The breaker is keyed by `a2a/<peer>`, so one
chronically-failing peer trips its own breaker without affecting others.

Loop prevention
---------------
We embed a hidden marker in the question text we send to the peer. The
peer's executor sees it via `is_peer_call(...)` and disables its own
`consult_peer` tool — so peers can be consulted but cannot themselves
consult, capping the call chain at depth 1.
"""

from __future__ import annotations

import logging
from time import perf_counter

from clients.a2a_client import A2AClient
from observability.context import ExecutionContext, get_context
from observability.reporter import (
    report_agent_completed,
    report_agent_failed,
    report_handoff,
    report_agent_started,
    report_token,
)
from .deadline import DeadlineExceeded
from .retry import CircuitOpenError

logger = logging.getLogger("multi_agent.peer")


# Embedded marker. Plain HTML comment so most LLMs ignore it semantically;
# our executor strips it before passing the text to the model.
PEER_CALL_MARKER = "<!-- a2a:peer-call depth=1 -->"

# Appended to a consulted agent's system prompt. A peer consultation is a
# focused sub-question from another agent — NOT a top-level deliverable — so
# the answer must be tight. Without this, a consulted agent (e.g. developer)
# answers with its full persona and can take 100s+ generating a whole report.
#
# The earlier version asked only for "brevity"; agents complied on tone but
# still dumped full code listings and blew past the token cap mid-sentence.
# This version sets a HARD length budget and bans code dumps — the actual
# cause of the truncations — so answers finish cleanly inside PEER_MAX_TOKENS.
PEER_BREVITY_INSTRUCTION = (
    "\n\n---\n## You are being CONSULTED by another agent\n"
    "This is a focused sub-question from a peer agent, not an end-user request. "
    "Answer ONLY the specific question asked. Hard rules:\n"
    "- Keep the entire answer less than 3000 tokens. Lead with the conclusion.\n"
    "- Use a short bullet list or a few tight sentences — no headers, no "
    "preamble, no restating the question.\n"
    "- Do NOT produce a full deliverable or a full code listing. At most ONE "
    "snippet of <=8 lines, and only if code is essential; otherwise describe "
    "the approach in prose.\n"
    "- If the topic is large, give the most important points and stop — "
    "do not trail off mid-thought.\n"
    "The calling agent will integrate your answer into its own. Brevity wins."
)

# Safety-net cap on a consulted agent's output so a single consult can't balloon
# unboundedly. Conciseness is enforced by PEER_BREVITY_INSTRUCTION (the PEER_MAX_TOKENS tokens
# budget); this cap is just a backstop, set generously so it effectively never
# binds — a peer answer ends because the model finished, not because it was cut
# mid-token.
PEER_MAX_TOKENS = 3000

# One shared client for all peer consultations (uses the configured default
# transport — native JSON-RPC unless overridden).
_client: A2AClient | None = None


def _peer_client() -> A2AClient:
    global _client
    if _client is None:
        _client = A2AClient()
    return _client


def is_peer_call(user_text: str | None) -> bool:
    """True if this invocation came from another agent (depth >= 1)."""
    return bool(user_text) and PEER_CALL_MARKER in user_text


def strip_peer_marker(user_text: str | None) -> str:
    if not user_text:
        return ""
    return user_text.replace(PEER_CALL_MARKER, "").strip()


async def consult_peer(
    peer_name: str,
    question: str,
    *,
    caller: str | None = None,
) -> str:
    """Invoke another A2A agent via the structured A2AClient.
        Diretly call the peer's /a2a endpoint, bypassing LiteLLM, to preserve streaming and avoid nested retries.
        The peer's executor sees the embedded marker and disables its own consult_peer tool, preventing infinite loops. 
        If an ExecutionContext is active, emits handoff / started / token / completed events for nested observability.
        Respects the same retry and circuit breaker policy as all A2A calls, with breaker keyed by peer name. Returns the peer's answer text, or an error message if the call fails or returns empty.
    """
    ctx = get_context()
    peer_ctx: ExecutionContext | None = None
    from_agent = caller or (ctx.workflow_name if ctx else None) or "unknown"

    if ctx is not None:
        peer_ctx = ctx.child(workflow_name=peer_name)
        await report_handoff(
            ctx, from_agent=from_agent, to_agent=peer_name,
            task=question[:200], to_span_id=peer_ctx.span_id,
        )
        await report_agent_started(peer_ctx, peer_name)

    wrapped = f"{question}\n\n{PEER_CALL_MARKER}"
    logger.info("peer.consult.start peer=%s question_chars=%d", peer_name, len(question))
    t0 = perf_counter()
    try:
        chunks: list[str] = []
        async for ev in _peer_client().stream(peer_name, wrapped, ctx=peer_ctx):
            if ev.text:
                chunks.append(ev.text)
                if peer_ctx:
                    await report_token(peer_ctx, peer_name, ev.text)
        content = "".join(chunks)
    except DeadlineExceeded:
        if peer_ctx:
            await report_agent_failed(peer_ctx, peer_name, error="deadline exceeded")
        raise
    except CircuitOpenError as exc:
        logger.warning("peer.consult.circuit_open peer=%s err=%s", peer_name, exc)
        if peer_ctx:
            await report_agent_failed(peer_ctx, peer_name, error=f"circuit open: {exc}")
        return (
            f"(consultation of `{peer_name}` skipped — circuit breaker "
            f"OPEN after repeated failures; please proceed without their input)"
        )
    except Exception as exc:
        logger.exception("peer.consult.failed peer=%s", peer_name)
        if peer_ctx:
            await report_agent_failed(peer_ctx, peer_name, error=str(exc))
        return f"(consultation of `{peer_name}` failed after retries: {exc})"

    latency = (perf_counter() - t0) * 1000

    if not content:
        logger.warning("peer.consult.empty peer=%s", peer_name)
        if peer_ctx:
            await report_agent_failed(peer_ctx, peer_name, error="empty response")
        return f"(consultation of `{peer_name}` returned an empty response)"

    if peer_ctx:
        await report_agent_completed(
            peer_ctx, peer_name, summary=content[:300], latency_ms=latency,
        )
    return content
