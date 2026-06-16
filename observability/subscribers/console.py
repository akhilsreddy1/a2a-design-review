"""Console subscriber — human-readable event logging.

Driven by typed events, so each log line is derived from structured fields
rather than a hand-written string. Tokens are high-volume and stay at DEBUG.
"""

from __future__ import annotations

import logging

from observability.event_bus import Subscription, event_bus
from observability.events import AgentHandoffEvent, BaseEvent, TokenStreamEvent

logger = logging.getLogger("multi_agent.console")


def _format(event: BaseEvent) -> str:
    short_trace = event.trace_id[:8]
    if isinstance(event, AgentHandoffEvent):
        extra = f" via {event.method}" if event.method else ""
        return f"[{short_trace}] handoff {event.from_agent} -> {event.to_agent}{extra} ({event.task})"
    if isinstance(event, TokenStreamEvent):
        return f"[{short_trace}] token {event.agent_name} (+{len(event.text)} chars)"
    return f"[{short_trace}] {event.event_type} {event.agent_name or ''}".rstrip()


def _on_event(event: BaseEvent) -> None:
    level = logging.DEBUG if isinstance(event, TokenStreamEvent) else logging.INFO
    logger.log(level, "EVENT %s", _format(event))


def register() -> Subscription:
    return event_bus.subscribe(_on_event)
