"""Execution context — the correlation envelope propagated through every layer.


Propagation
-----------
- **In-process**: a ``contextvars.ContextVar`` (task-local, NOT a global
  mutable) carries the "current" context so emitters can stamp events
  without threading it through every signature. Use :func:`bind_context`
  around a unit of work.
- **Across A2A / HTTP**: serialize with :meth:`to_headers` and rebuild on
  the far side with :meth:`from_headers`, or embed inside a structured
  request body (NOT in the LLM prompt).

Ported from the agent-a2a reference; same shape, same semantics — the
observability seam intentionally has no project-specific assumptions so
it can be reused as-is across our multi-agent mesh.
"""

from __future__ import annotations

import contextlib
import contextvars
from uuid import uuid4

from pydantic import BaseModel

# Header names used to carry context across A2A / HTTP boundaries.
HEADER_PREFIX = "x-exec-"


class ExecutionContext(BaseModel):
    """Correlation context. Use :meth:`child` to descend a span."""

    tenant_id: str | None = None
    user_id: str | None = None

    session_id: str
    conversation_id: str
    trace_id: str

    span_id: str
    parent_span_id: str | None = None

    workflow_name: str | None = None

    # ── Construction ────────────────────────────────────────────────────────
    @classmethod
    def new_root(
        cls,
        *,
        conversation_id: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        tenant_id: str | None = None,
        workflow_name: str | None = None,
    ) -> "ExecutionContext":
        """Start a new trace (root span) for one user request."""
        conversation_id = conversation_id or str(uuid4())
        return cls(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id or conversation_id,
            conversation_id=conversation_id,
            trace_id=str(uuid4()),
            span_id=str(uuid4()),
            parent_span_id=None,
            workflow_name=workflow_name,
        )

    def child(self, *, workflow_name: str | None = None) -> "ExecutionContext":
        """Derive a child span: same trace, new span_id, parent = this span."""
        return self.model_copy(
            update={
                "span_id": str(uuid4()),
                "parent_span_id": self.span_id,
                "workflow_name": workflow_name or self.workflow_name,
            }
        )

    # ── Cross-process propagation ───────────────────────────────────────────
    def to_headers(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for key, value in self.model_dump().items():
            if value is not None:
                out[f"{HEADER_PREFIX}{key.replace('_', '-')}"] = str(value)
        return out

    @classmethod
    def from_headers(cls, headers: dict[str, str]) -> "ExecutionContext | None":
        lowered = {k.lower(): v for k, v in headers.items()}
        fields: dict[str, str] = {}
        for key in cls.model_fields:
            header = f"{HEADER_PREFIX}{key.replace('_', '-')}"
            if header in lowered:
                fields[key] = lowered[header]
        required = {"session_id", "conversation_id", "trace_id", "span_id"}
        if not required.issubset(fields):
            return None
        return cls(**fields)


# ── In-process propagation (task-local) ──────────────────────────────────────
_current: contextvars.ContextVar[ExecutionContext | None] = contextvars.ContextVar(
    "execution_context", default=None
)


def get_context() -> ExecutionContext | None:
    return _current.get()


def set_context(ctx: ExecutionContext | None) -> contextvars.Token:
    return _current.set(ctx)


@contextlib.contextmanager
def bind_context(ctx: ExecutionContext):
    """Bind ``ctx`` as the current context for the duration of the block."""
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)
