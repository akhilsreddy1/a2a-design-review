"""Event reporter — lets agent processes push typed events to the bridge.

Agents run in separate processes (containers) and don't share the bridge's
in-memory event bus. When an agent does something worth observing — like
consulting a peer — it calls :func:`report_event` (or the convenience
helpers) which POSTs a typed event dict to ``POST /api/events`` on the
bridge. The bridge validates and republishes onto its bus, so the SSE
stream, JSONL traces, and console subscriber all see nested spans.

This is the HTTP replacement for the reference's ``FlowLogSource`` (which
tailed a shared JSONL file). Same idea — cross-process event capture —
but over HTTP so containers don't need a shared volume.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from config import get_settings
from observability.context import ExecutionContext
from observability.events import (
    AgentCompletedEvent,
    AgentFailedEvent,
    AgentHandoffEvent,
    AgentStartedEvent,
    TokenStreamEvent,
    ToolCallCompletedEvent,
    ToolCallStartedEvent,
)

logger = logging.getLogger("multi_agent.reporter")

_REPORT_TIMEOUT = 5.0


async def report_event(event_dict: dict[str, Any]) -> bool:
    """POST a serialized event to the bridge. Best-effort, never raises."""
    """ This bridges the gap between agent processes and the bridge's in-memory event bus ,by doing an HTTP-ingest of envents to the bridge"""

    url = f"{get_settings().bridge_url.rstrip('/')}/api/events"
    try:
        async with httpx.AsyncClient(timeout=_REPORT_TIMEOUT) as client:
            resp = await client.post(url, json=event_dict)
            if resp.status_code >= 400:
                logger.warning("reporter.rejected status=%s body=%s", resp.status_code, resp.text[:200])
                return False
            return True
    except Exception:
        logger.debug("reporter.failed url=%s", url, exc_info=True)
        return False


async def report_handoff(
    ctx: ExecutionContext,
    *,
    from_agent: str,
    to_agent: str,
    task: str | None = None,
    to_span_id: str | None = None,
) -> None:
    event = AgentHandoffEvent.from_context(
        ctx,
        agent_name=to_agent,
        from_agent=from_agent,
        to_agent=to_agent,
        task=task,
        method="peer",
        to_span_id=to_span_id,
    )
    await report_event(event.to_json())


async def report_agent_started(ctx: ExecutionContext, agent_name: str) -> None:
    event = AgentStartedEvent.from_context(ctx, agent_name=agent_name)
    await report_event(event.to_json())


async def report_agent_completed(
    ctx: ExecutionContext,
    agent_name: str,
    *,
    summary: str | None = None,
    latency_ms: float = 0,
) -> None:
    event = AgentCompletedEvent.from_context(
        ctx, agent_name=agent_name, summary=summary, latency_ms=latency_ms,
    )
    await report_event(event.to_json())


async def report_agent_failed(
    ctx: ExecutionContext, agent_name: str, *, error: str,
) -> None:
    event = AgentFailedEvent.from_context(
        ctx, agent_name=agent_name, error=error,
    )
    await report_event(event.to_json())


async def report_token(
    ctx: ExecutionContext, agent_name: str, text: str,
) -> None:
    """Report a token chunk from a peer agent's streamed answer."""
    if not text:
        return
    event = TokenStreamEvent.from_context(ctx, agent_name=agent_name, text=text)
    await report_event(event.to_json())


async def report_tool_started(
    ctx: ExecutionContext,
    agent_name: str,
    tool_name: str,
    args: dict[str, Any] | None = None,
) -> None:
    event = ToolCallStartedEvent.from_context(
        ctx, agent_name=agent_name, tool_name=tool_name, args=args or {},
    )
    await report_event(event.to_json())


async def report_tool_completed(
    ctx: ExecutionContext,
    agent_name: str,
    tool_name: str,
    *,
    latency_ms: float = 0,
    ok: bool = True,
) -> None:
    event = ToolCallCompletedEvent.from_context(
        ctx, agent_name=agent_name, tool_name=tool_name,
        latency_ms=latency_ms, ok=ok,
    )
    await report_event(event.to_json())
