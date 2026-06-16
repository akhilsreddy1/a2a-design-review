"""Resilience layer for LLM/A2A calls — retries + circuit breakers.

What this provides
------------------
- **Exponential backoff with full jitter** for transient errors (network blips,
  429s, 5xx, timeouts).
- **Per-target circuit breaker** so a flapping model alias (or a flapping peer
  agent invoked via `a2a/<name>`) doesn't drag every request down with it.
  CLOSED → trips OPEN after N consecutive failures → HALF_OPEN after a cool-
  down → CLOSED on a successful trial.
- A simple `is_retryable()` classifier that distinguishes transient failures
  (worth retrying) from semantic ones (wrong model, bad payload — don't retry).

Tunables (env vars; sensible defaults if unset)
-----------------------------------------------
    LLM_MAX_RETRIES            total attempts incl. first       default 3
    LLM_RETRY_BASE_DELAY       seconds for the first wait       default 0.5
    LLM_RETRY_MAX_DELAY        upper bound per wait             default 8.0
    LLM_RETRY_JITTER           "true" / "false"                 default true
    LLM_BREAKER_THRESHOLD      consecutive failures to OPEN     default 5
    LLM_BREAKER_RESET          seconds before HALF_OPEN trial   default 30.0

A `target` string scopes a breaker (typically the model alias, e.g.
``"claude-opus-4-6"`` or ``"a2a/security"``).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

from config import get_settings
from .deadline import DeadlineExceeded, check_deadline, remaining

logger = logging.getLogger("multi_agent.retry")

T = TypeVar("T")


# ---------------------------------------------------------------------------
#  Retry policy
# ---------------------------------------------------------------------------


@dataclass
class RetryPolicy:
    """Exponential backoff with optional full jitter."""

    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0
    use_jitter: bool = True

    @classmethod
    def from_env(cls) -> "RetryPolicy":
        settings = get_settings()
        return cls(
            max_attempts=settings.llm_max_retries,
            base_delay=settings.llm_retry_base_delay,
            max_delay=settings.llm_retry_max_delay,
            use_jitter=settings.llm_retry_jitter,
        )

    def delay_for(self, attempt: int) -> float:
        """Return seconds to wait BEFORE attempt+1 (attempt is 1-based)."""
        exp = self.base_delay * (2 ** (attempt - 1))
        capped = min(exp, self.max_delay)
        return random.uniform(0, capped) if self.use_jitter else capped


# ---------------------------------------------------------------------------
#  Circuit breaker
# ---------------------------------------------------------------------------


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 5
    reset_timeout: float = 30.0

    @classmethod
    def from_env(cls) -> "CircuitBreakerConfig":
        settings = get_settings()
        return cls(
            failure_threshold=settings.llm_breaker_threshold,
            reset_timeout=settings.llm_breaker_reset,
        )


class CircuitOpenError(RuntimeError):
    """Raised when a call is short-circuited because the breaker is OPEN."""


class CircuitBreaker:
    """Per-target circuit breaker — async-safe, single-process.

    State machine:
        CLOSED    pass-through; count consecutive failures.
        OPEN      fail fast for `reset_timeout` seconds.
        HALF_OPEN allow ONE trial; success → CLOSED, failure → OPEN.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self, name: str, config: CircuitBreakerConfig | None = None) -> None:
        self.name = name
        self.config = config or CircuitBreakerConfig.from_env()
        self.state: str = self.CLOSED
        self.failures: int = 0
        self.opened_at: float = 0.0
        self._lock = asyncio.Lock()

    async def before_call(self) -> None:
        """Either allow the call or raise CircuitOpenError."""
        async with self._lock:
            if self.state == self.OPEN:
                if time.monotonic() - self.opened_at >= self.config.reset_timeout:
                    self.state = self.HALF_OPEN
                    logger.info("circuit.half_open name=%s", self.name)
                else:
                    raise CircuitOpenError(
                        f"circuit `{self.name}` is OPEN "
                        f"({self.failures} consecutive failures)"
                    )

    async def on_success(self) -> None:
        async with self._lock:
            if self.state == self.HALF_OPEN or self.failures > 0:
                logger.info("circuit.closed name=%s recovered=true", self.name)
            self.state = self.CLOSED
            self.failures = 0

    async def on_failure(self) -> None:
        async with self._lock:
            self.failures += 1
            if self.state == self.HALF_OPEN:
                self.state = self.OPEN
                self.opened_at = time.monotonic()
                logger.warning(
                    "circuit `%s` → OPEN (HALF_OPEN trial failed)", self.name,
                )
            elif self.failures >= self.config.failure_threshold:
                self.state = self.OPEN
                self.opened_at = time.monotonic()
                logger.warning(
                    "circuit `%s` → OPEN (%d consecutive failures, reset in %.0fs)",
                    self.name, self.failures, self.config.reset_timeout,
                )

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "failures": self.failures,
            "opened_at": self.opened_at,
        }


_BREAKERS: dict[str, CircuitBreaker] = {}
_BREAKERS_LOCK = asyncio.Lock()


async def get_breaker(name: str) -> CircuitBreaker:
    """Fetch (or create on first use) the breaker for a target."""
    async with _BREAKERS_LOCK:
        if name not in _BREAKERS:
            _BREAKERS[name] = CircuitBreaker(name)
        return _BREAKERS[name]


# ---------------------------------------------------------------------------
#  Classifier — what counts as transient
# ---------------------------------------------------------------------------


# HTTP statuses we treat as transient (worth retrying).
_RETRYABLE_HTTP = {408, 425, 429, 500, 502, 503, 504}

# Import once at module level so the hot path doesn't re-import.
try:  # openai>=1.x
    from openai import (
        APIConnectionError as _OAIConnErr,
        APITimeoutError as _OAITimeoutErr,
        APIStatusError as _OAIStatusErr,
        InternalServerError as _OAIInternalErr,
        RateLimitError as _OAIRateErr,
    )
except Exception:  # pragma: no cover
    _OAIConnErr = _OAITimeoutErr = _OAIStatusErr = _OAIInternalErr = _OAIRateErr = ()  # type: ignore

try:
    import httpx as _httpx
    _HTTPX_TRANSIENT = (
        _httpx.ConnectError,
        _httpx.ReadTimeout,
        _httpx.WriteTimeout,
        _httpx.PoolTimeout,
        _httpx.RemoteProtocolError,
    )
except Exception:  # pragma: no cover
    _HTTPX_TRANSIENT = ()  # type: ignore


def is_retryable(exc: BaseException) -> bool:
    """Decide whether an exception is transient and worth another attempt."""
    # Cancellation must NEVER be retried — it propagates.
    if isinstance(exc, asyncio.CancelledError):
        return False
    if isinstance(exc, (_OAIConnErr, _OAITimeoutErr, _OAIInternalErr, _OAIRateErr)):
        return True
    if isinstance(exc, _OAIStatusErr):
        return getattr(exc, "status_code", None) in _RETRYABLE_HTTP
    if isinstance(exc, _HTTPX_TRANSIENT):
        return True
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return True
    return False


# ---------------------------------------------------------------------------
#  with_retry — the driver
# ---------------------------------------------------------------------------


async def with_retry(
    factory: Callable[[], Awaitable[T]],
    *,
    target: str,
    policy: RetryPolicy | None = None,
) -> T:
    """Run `factory()` with retry + circuit-breaker semantics.

    `factory` is a NO-ARG async callable that returns a *fresh* coroutine
    each call — we may call it more than once. (You can't await the same
    coroutine twice.)
    """
    pol = policy or RetryPolicy.from_env()
    breaker = await get_breaker(target)

    last_exc: BaseException | None = None
    for attempt in range(1, pol.max_attempts + 1):
        # Past the overall deadline? Don't even start another attempt.
        check_deadline()
        # Short-circuit if the breaker is OPEN. Don't retry past this.
        await breaker.before_call()

        try:
            result = await factory()
        except asyncio.CancelledError:
            # Honor cancellation immediately.
            raise
        except DeadlineExceeded:
            # Hard ceiling — do NOT retry and do NOT record as a target
            # failure (the target is healthy; we just ran out of time).
            raise
        except BaseException as exc:
            last_exc = exc
            await breaker.on_failure()
            transient = is_retryable(exc)
            if not transient or attempt >= pol.max_attempts:
                raise
            # Clamp backoff so we don't sleep past the overall deadline.
            delay = pol.delay_for(attempt)
            rem = remaining()
            if rem is not None:
                if rem <= 0:
                    raise DeadlineExceeded("deadline exceeded between retries")
                # leave a small slice for the next attempt
                delay = max(0.0, min(delay, rem - 0.05))
                if delay == 0.0:
                    raise DeadlineExceeded("no time left for another attempt")
            logger.warning(
                "[retry] target=%s attempt=%d/%d failed (%s: %s); sleeping %.2fs",
                target, attempt, pol.max_attempts,
                type(exc).__name__, str(exc)[:200], delay,
            )
            await asyncio.sleep(delay)
            continue

        await breaker.on_success()
        return result

    # Defensive: loop exit without return or raise (shouldn't happen).
    assert last_exc is not None
    raise last_exc
