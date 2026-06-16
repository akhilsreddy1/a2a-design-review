"""Typed orchestration events.

Every event is a Pydantic model carrying the full correlation envelope
(session / conversation / trace / span), so any subscriber can persist,
route, or visualize it without parsing free-form strings. All events
serialize to JSON via ``model_dump(mode="json")``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from .context import ExecutionContext


def _now() -> datetime:
    return datetime.now(timezone.utc)


class BaseEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = Field(default_factory=_now)

    # Correlation envelope (copied from ExecutionContext)
    session_id: str
    conversation_id: str
    trace_id: str
    span_id: str
    parent_span_id: str | None = None

    tenant_id: str | None = None
    user_id: str | None = None

    event_type: str
    agent_name: str | None = None

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_context(cls, ctx: ExecutionContext, **fields: Any) -> "BaseEvent":
        """Build an event of this class, stamping the correlation envelope."""
        return cls(
            session_id=ctx.session_id,
            conversation_id=ctx.conversation_id,
            trace_id=ctx.trace_id,
            span_id=ctx.span_id,
            parent_span_id=ctx.parent_span_id,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            **fields,
        )


# ── Workflow lifecycle ───────────────────────────────────────────────────────
class WorkflowStartedEvent(BaseEvent):
    event_type: Literal["workflow_started"] = "workflow_started"
    workflow_name: str | None = None
    query: str | None = None


class WorkflowCompletedEvent(BaseEvent):
    event_type: Literal["workflow_completed"] = "workflow_completed"
    workflow_name: str | None = None
    summary: str | None = None
    latency_ms: float = 0


# ── Agent lifecycle ──────────────────────────────────────────────────────────
class AgentStartedEvent(BaseEvent):
    event_type: Literal["agent_started"] = "agent_started"


class AgentCompletedEvent(BaseEvent):
    event_type: Literal["agent_completed"] = "agent_completed"
    summary: str | None = None
    latency_ms: float = 0


class AgentFailedEvent(BaseEvent):
    event_type: Literal["agent_failed"] = "agent_failed"
    error: str = ""


class AgentHandoffEvent(BaseEvent):
    event_type: Literal["agent_handoff"] = "agent_handoff"
    from_agent: str
    to_agent: str
    task: str | None = None
    # Routing context — useful for the UI badge.
    method: str | None = None       # "llm" / "keyword" / "pinned" / "default" / "peer"
    confidence: float | None = None
    reason: str | None = None
    # The span_id of the agent being handed to. Lets the UI link this handoff
    # line to that agent's answer tokens (which carry the same span_id).
    to_span_id: str | None = None


# ── Token streaming ──────────────────────────────────────────────────────────
class TokenStreamEvent(BaseEvent):
    event_type: Literal["token_stream"] = "token_stream"
    text: str = ""


# ── Tool calls (peer consult, future tool use) ───────────────────────────────
class ToolCallStartedEvent(BaseEvent):
    event_type: Literal["tool_call_started"] = "tool_call_started"
    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)


class ToolCallCompletedEvent(BaseEvent):
    event_type: Literal["tool_call_completed"] = "tool_call_completed"
    tool_name: str
    latency_ms: float = 0
    ok: bool = True


# Registry for deserializing persisted events back into typed models (replay).
EVENT_TYPES: dict[str, type[BaseEvent]] = {
    cls.model_fields["event_type"].default: cls  # type: ignore[misc]
    for cls in (
        WorkflowStartedEvent,
        WorkflowCompletedEvent,
        AgentStartedEvent,
        AgentCompletedEvent,
        AgentFailedEvent,
        AgentHandoffEvent,
        TokenStreamEvent,
        ToolCallStartedEvent,
        ToolCallCompletedEvent,
    )
}


def parse_event(data: dict[str, Any]) -> BaseEvent:
    """Rebuild a typed event from a serialized dict (used for JSONL replay)."""
    cls = EVENT_TYPES.get(data.get("event_type", ""), BaseEvent)
    return cls(**data)
