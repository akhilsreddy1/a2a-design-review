"""RouterOrchestrator — the one canonical orchestration path.

Responsibilities (every step emits typed events; nothing bypasses the bus):

  1. **Discovery**     — pull live agent cards from the LiteLLM registry.
  2. **Selection**     — reuse :func:`router.classifier.route` (LLM → keyword
                         → default) to pick one agent, with its decision
                         metadata (method / confidence / reason).
  3. **Fallback**      — `route` already degrades to keyword then default;
                         a pinned agent short-circuits selection entirely.
  4. **Handoff event** — emit orchestrator → agent handoff with the routing
                         decision attached.
  5. **Invocation**    — *stream* the agent through the structured
                         :class:`A2AClient` (``message/send`` with
                         transport-level streaming and fallback to a single
                         response). Each chunk is emitted as a ``token_stream``
                         event so the UI renders incrementally.
  6. **Answer**        — close the agent and workflow spans once the stream
                         is exhausted or the full response arrives.
  7. **Envelope**      — return an :class:`OrchestrationResult` for callers
                         that want the whole picture synchronously.

The bridge (`api/server.py`) calls :meth:`run` inside a task and never sees
agent internals — it only observes the bus.
"""

from __future__ import annotations

import logging
from time import perf_counter
from typing import Any

from clients.a2a_client import A2AClient
from models.a2a import AgentCardModel, AgentResult, OrchestrationResult
from observability.context import ExecutionContext, bind_context
from observability.emitters import (
    emit_agent_completed,
    emit_agent_failed,
    emit_agent_started,
    emit_handoff,
    emit_token,
    emit_workflow_completed,
    emit_workflow_started,
)
from registry import LiteLLMRegistry
from router.classifier import route

logger = logging.getLogger("multi_agent.orchestrator")

ORCHESTRATOR_ID = "orchestrator"


class RouterOrchestrator:
    """Discovers agents, routes by capability, invokes over A2A, emits events."""

    def __init__(
        self,
        *,
        registry: LiteLLMRegistry | None = None,
        a2a: A2AClient | None = None,
    ) -> None:
        self.registry = registry or LiteLLMRegistry()
        self.a2a = a2a or A2AClient()

    # ── public API ──────────────────────────────────────────────────────────
    async def run(
        self,
        ctx: ExecutionContext,
        query: str,
        *,
        pinned_agent: str | None = None,
    ) -> OrchestrationResult:
        """Drive one run end-to-end, emitting typed events the whole way."""
        with bind_context(ctx):
            t0 = perf_counter()
            await emit_workflow_started(ctx, query=query)

            # 1. discovery
            try:
                cards = await self.registry.discover_cards()
            except Exception as exc:
                logger.exception("orchestration.discovery_failed trace_id=%s", ctx.trace_id)
                return await self._fail(ctx, ORCHESTRATOR_ID, f"Discovery failed: {exc}", t0, query)

            if not cards:
                return await self._fail(ctx, ORCHESTRATOR_ID, "No agents discovered.", t0, query)

            # 2 & 3. selection (+ fallback handled inside route / pin)
            decision = await self._select(query, cards, pinned_agent)
            card = self._card_by_name(cards, decision["agent_id"])
            if card is None:
                return await self._fail(
                    ctx, ORCHESTRATOR_ID,
                    f"Router selected unknown agent `{decision['agent_id']}`.",
                    t0, query,
                )

            target = card.name
            target_ctx = ctx.child(workflow_name=target)  # the agent's span

            # 4. handoff orchestrator → agent (routing span closes here)
            await emit_handoff(
                ctx,
                from_agent=ORCHESTRATOR_ID,
                to_agent=target,
                task=query,
                method=decision.get("method"),
                confidence=decision.get("confidence"),
                reason=decision.get("reason"),
                to_span_id=target_ctx.span_id,
            )
            await emit_agent_completed(ctx, ORCHESTRATOR_ID)
            await emit_agent_started(target_ctx, target)

            # 5. stream the agent's answer DIRECTLY from its /a2a endpoint
            
            chunks: list[str] = []
            final_artifact = ""
            saw_delta = False
            failed = False
            error: str | None = None
            try:
                async for event in self.a2a.stream_agent(
                    card.url, query, agent_name=target, ctx=target_ctx,
                ):
                    if event.kind == "error":
                        failed = True
                        error = event.text or "A2A stream error"
                        await emit_agent_failed(target_ctx, target, error=error)
                        break
                    # Answer deltas: progress-phase status updates, or a
                    # whole-chunk token from the send() fallback.
                    # Reading from A2A stream and then emitting on the bus separately allows the UI to render incrementally.x
                    if event.text and (event.phase == "progress" or event.kind == "token"):
                        saw_delta = True
                        chunks.append(event.text)
                        await emit_token(target_ctx, target, event.text)
                    elif event.kind == "artifact" and event.text:
                        # Final consolidation — keep as backup, don't double-emit.
                        final_artifact = event.text
                    # else: lifecycle (working/consulting/completed) — skip.
            except Exception as exc:
                logger.exception("orchestration.invoke_failed trace_id=%s agent=%s", ctx.trace_id, target)
                await emit_agent_failed(target_ctx, target, error=str(exc))
                return await self._finish(
                    ctx, target_ctx, target, decision, "".join(chunks),
                    (perf_counter() - t0) * 1000, cards,
                    failed=True, error=str(exc),
                )

            content = "".join(chunks)
            # Nothing streamed (e.g. agent only emitted a final artifact) →
            # emit the consolidation once so the UI still gets the answer.
            if not saw_delta and final_artifact:
                content = final_artifact
                await emit_token(target_ctx, target, content)
            latency = (perf_counter() - t0) * 1000

            # 6. close spans
            return await self._finish(
                ctx, target_ctx, target, decision, content, latency, cards,
                failed=failed, error=error,
            )

    # ── selection ───────────────────────────────────────────────────────────
    async def _select(
        self,
        query: str,
        cards: list[AgentCardModel],
        pinned_agent: str | None,
    ) -> dict[str, Any]:
        if pinned_agent:
            if any(c.name == pinned_agent for c in cards):
                return {
                    "agent_id": pinned_agent,
                    "method": "pinned",
                    "confidence": 1.0,
                    "reason": f"User pinned the conversation to `{pinned_agent}`.",
                }
            logger.warning(
                "select.pinned_agent_invalid agent=%s available=%s",
                pinned_agent, [c.name for c in cards],
            )
        agent_dicts = [self._card_to_route_dict(c) for c in cards]
        
        rd = await route(query, agents=agent_dicts)
        return rd.to_dict()

    @staticmethod
    def _card_to_route_dict(card: AgentCardModel) -> dict[str, Any]:
        """Shape a card the way router.classifier.route expects."""
        return {
            "id": card.name,
            "role": card.role or "specialist",
            "description": card.description,
            "expertise": card.metadata.get("expertise", []),
            "skills": [
                {"name": s.name, "tags": s.tags, "examples": s.examples}
                for s in card.skills
            ],
        }

    @staticmethod
    def _card_by_name(cards: list[AgentCardModel], name: str) -> AgentCardModel | None:
        return next((c for c in cards if c.name == name), None)

    # ── result / failure helpers ────────────────────────────────────────────
    async def _finish(
        self,
        ctx: ExecutionContext,
        target_ctx: ExecutionContext,
        target: str,
        decision: dict[str, Any],
        content: str,
        latency: float,
        cards: list[AgentCardModel],
        *,
        failed: bool = False,
        error: str | None = None,
    ) -> OrchestrationResult:
        summary = (content.strip() or "(no content returned)")[:400]
        if not failed:
            await emit_agent_completed(target_ctx, target, summary=summary, latency_ms=latency)
        await emit_workflow_completed(ctx, summary=summary, latency_ms=latency)
        return self._result(
            ctx, target, decision, content, latency, cards,
            failed=failed, error=error,
        )

    async def _fail(
        self,
        ctx: ExecutionContext,
        who: str,
        error: str,
        t0: float,
        query: str,
    ) -> OrchestrationResult:
        await emit_agent_failed(ctx, who, error=error)
        latency = (perf_counter() - t0) * 1000
        await emit_workflow_completed(ctx, summary=error[:400], latency_ms=latency)
        return OrchestrationResult(
            run_id=ctx.trace_id,
            conversation_id=ctx.conversation_id,
            trace_id=ctx.trace_id,
            selected_agent=who,
            final_answer="",
            method="error",
            reason=error,
            latency_ms=latency,
        )

    def _result(
        self,
        ctx: ExecutionContext,
        target: str,
        decision: dict[str, Any],
        content: str,
        latency: float,
        cards: list[AgentCardModel],
        *,
        failed: bool = False,
        error: str | None = None,
    ) -> OrchestrationResult:
        return OrchestrationResult(
            run_id=ctx.trace_id,
            conversation_id=ctx.conversation_id,
            trace_id=ctx.trace_id,
            selected_agent=target,
            final_answer=content,
            method=decision.get("method"),
            confidence=decision.get("confidence"),
            reason=decision.get("reason"),
            latency_ms=latency,
            agent_results=[
                AgentResult(
                    agent_name=target,
                    task_id=ctx.trace_id,
                    status="failed" if failed else "completed",
                    content=content,
                    latency_ms=latency,
                    error=error,
                )
            ],
            discovered_agents=[c.name for c in cards],
        )
