"""Per-request wall-clock deadlines, propagated via contextvars.

What this fixes
---------------
The retry/backoff layer gives every individual LLM call a per-attempt
timeout — but with 3 retries and 60s timeouts and 4 tool-loop rounds,
the worst-case wall-clock for one A2A task is multi-minute. A peer that
hangs at the network layer can keep the calling agent stuck.

How it works
------------
A contextvar (`_DEADLINE_AT`) holds the monotonic clock time at which
the current request must be done. Entering `async with deadline(secs):`
installs the deadline AND wraps the body in `asyncio.timeout(secs)` for
a hard ceiling — when the timer fires, asyncio cancels the inner work
and we surface a clean `DeadlineExceeded` to the caller.

Every layer that does I/O calls `remaining_or(default)` to shrink its
own timeout so it can never outlive the deadline:

    eff_timeout = remaining_or(LLM_TIMEOUT)
    client = make_async_client(timeout=eff_timeout)

Tunable: ``A2A_TASK_DEADLINE`` env var (default 180s).

Cross-process propagation
-------------------------
The caller sets an ``x-deadline-remaining`` HTTP header with the number of
seconds left in its budget. The callee reads it via :func:`deadline_from_header`
and installs ``min(own_default, caller_remaining)`` as its ceiling — so a peer
agent can never run longer than the caller's remaining budget.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from contextlib import asynccontextmanager

logger = logging.getLogger("multi_agent.deadline")

from config import get_settings

# Default overall deadline for one A2A task, in seconds.
DEFAULT_TASK_DEADLINE = get_settings().a2a_task_deadline

DEADLINE_HEADER = "x-deadline-remaining"

# Monotonic deadline (None = no deadline active in this context).
_DEADLINE_AT: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "a2a_deadline_at", default=None
)


class DeadlineExceeded(RuntimeError):
    """Raised when the per-request wall-clock budget runs out.

    Distinct from `asyncio.TimeoutError` so callers can tell the difference
    between "this one HTTP call timed out" (transient, may retry) and
    "the overall task budget is gone" (non-transient, fail the task).
    """


def remaining() -> float | None:
    """Seconds remaining in the current deadline.

    Returns:
        None  — no deadline active in this context.
        > 0   — that many seconds left.
        <= 0  — deadline already passed.
    """
    deadline_at = _DEADLINE_AT.get()
    if deadline_at is None:
        return None
    return deadline_at - time.monotonic()


def check_deadline() -> None:
    """Raise `DeadlineExceeded` if the deadline has passed."""
    rem = remaining()
    if rem is not None and rem <= 0:
        raise DeadlineExceeded(f"task deadline exceeded by {-rem:.2f}s")


def remaining_or(default: float) -> float:
    """Return `min(default, remaining_seconds)`.

    - If no deadline is set, returns `default`.
    - If the deadline has already passed, returns a tiny positive value
      (0.001s) so a downstream timeout fires immediately rather than
      passing 0 to something that treats it as "no timeout."
    """
    rem = remaining()
    if rem is None:
        return default
    if rem <= 0:
        return 0.001
    return min(default, rem)


@asynccontextmanager
async def deadline(seconds: float | None = None):
    """Install a deadline and enforce it as a hard wall-clock ceiling.

    Usage:
        async with deadline():          # uses A2A_TASK_DEADLINE
            ... work ...
        async with deadline(30):        # explicit 30s budget
            ... work ...

    If the timer fires, inner work is cancelled and `DeadlineExceeded`
    is raised on exit. The contextvar is always reset on exit so the
    deadline doesn't leak into surrounding code.

    If `seconds <= 0` we DON'T enforce a ceiling (debugging escape hatch);
    callers downstream just see `remaining()` return None.
    """
    secs = seconds if seconds is not None else DEFAULT_TASK_DEADLINE
    if secs is None or secs <= 0:
        # Escape hatch: no enforcement, no contextvar set.
        yield
        return

    token = _DEADLINE_AT.set(time.monotonic() + secs)
    try:
        async with asyncio.timeout(secs):
            yield
    except (asyncio.TimeoutError, TimeoutError) as exc:
        raise DeadlineExceeded(
            f"task deadline of {secs:.0f}s exceeded"
        ) from exc
    finally:
        _DEADLINE_AT.reset(token)


def deadline_header_value() -> str | None:
    """Return the ``x-deadline-remaining`` header value, or None if no deadline."""
    rem = remaining()
    if rem is None:
        return None
    return f"{max(rem, 0):.3f}"


def effective_deadline(caller_remaining: float | None) -> float:
    """Compute the deadline an agent should install.

    Returns ``min(DEFAULT_TASK_DEADLINE, caller_remaining)`` — the callee
    can never run longer than the caller's remaining budget.  A small
    buffer (1s) is subtracted from the caller value to account for
    network round-trip overhead.
    """
    own = DEFAULT_TASK_DEADLINE
    if caller_remaining is None or caller_remaining <= 0:
        return own
    return min(own, max(caller_remaining - 1.0, 0.5))


def parse_deadline_header(headers: dict[str, str]) -> float | None:
    """Extract the caller's remaining budget from HTTP headers."""
    raw = headers.get(DEADLINE_HEADER) or headers.get(DEADLINE_HEADER.lower())
    if not raw:
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None
