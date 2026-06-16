"""Emitter helpers — publish typed events from an ExecutionContext.

Orchestration code calls these instead of writing log strings. Each helper
stamps the correlation envelope from the supplied context and publishes
to the global bus, where subscribers (SSE, JSONL, console) react
independently.
"""

from __future__ import annotations

from .context import ExecutionContext
from .event_bus import event_bus
from .events import (
    AgentCompletedEvent,
    AgentFailedEvent,
    AgentHandoffEvent,
    AgentStartedEvent,
    TokenStreamEvent,
    ToolCallCompletedEvent,
    ToolCallStartedEvent,
    WorkflowCompletedEvent,
    WorkflowStartedEvent,
)


async def emit_workflow_started(
    ctx: ExecutionContext, *, query: str | None = None
) -> None:
    await event_bus.publish(
        WorkflowStartedEvent.from_context(
            ctx,
            agent_name=ctx.workflow_name,
            workflow_name=ctx.workflow_name,
            query=query,
        )
    )


async def emit_workflow_completed(
    ctx: ExecutionContext, *, summary: str | None = None, latency_ms: float = 0
) -> None:
    await event_bus.publish(
        WorkflowCompletedEvent.from_context(
            ctx,
            agent_name=ctx.workflow_name,
            workflow_name=ctx.workflow_name,
            summary=summary,
            latency_ms=latency_ms,
        )
    )


async def emit_agent_started(ctx: ExecutionContext, agent_name: str) -> None:
    await event_bus.publish(
        AgentStartedEvent.from_context(ctx, agent_name=agent_name)
    )


async def emit_agent_completed(
    ctx: ExecutionContext,
    agent_name: str,
    *,
    summary: str | None = None,
    latency_ms: float = 0,
) -> None:
    await event_bus.publish(
        AgentCompletedEvent.from_context(
            ctx, agent_name=agent_name, summary=summary, latency_ms=latency_ms
        )
    )


async def emit_agent_failed(
    ctx: ExecutionContext, agent_name: str, *, error: str
) -> None:
    await event_bus.publish(
        AgentFailedEvent.from_context(ctx, agent_name=agent_name, error=error)
    )


async def emit_handoff(
    ctx: ExecutionContext,
    *,
    from_agent: str,
    to_agent: str,
    task: str | None = None,
    method: str | None = None,
    confidence: float | None = None,
    reason: str | None = None,
    to_span_id: str | None = None,
) -> None:
    await event_bus.publish(
        AgentHandoffEvent.from_context(
            ctx,
            agent_name=to_agent,
            from_agent=from_agent,
            to_agent=to_agent,
            task=task,
            method=method,
            confidence=confidence,
            reason=reason,
            to_span_id=to_span_id,
        )
    )


async def emit_token(ctx: ExecutionContext, agent_name: str, text: str) -> None:
    if not text:
        return
    await event_bus.publish(
        TokenStreamEvent.from_context(ctx, agent_name=agent_name, text=text)
    )


async def emit_tool_started(
    ctx: ExecutionContext, agent_name: str, tool_name: str, args: dict | None = None
) -> None:
    await event_bus.publish(
        ToolCallStartedEvent.from_context(
            ctx, agent_name=agent_name, tool_name=tool_name, args=args or {}
        )
    )


async def emit_tool_completed(
    ctx: ExecutionContext,
    agent_name: str,
    tool_name: str,
    *,
    latency_ms: float = 0,
    ok: bool = True,
) -> None:
    await event_bus.publish(
        ToolCallCompletedEvent.from_context(
            ctx,
            agent_name=agent_name,
            tool_name=tool_name,
            latency_ms=latency_ms,
            ok=ok,
        )
    )
