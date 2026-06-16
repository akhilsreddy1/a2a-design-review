"""Thin LLM-call helpers for an agent's OWN reasoning (and the router).

Scope (important): this module is for an agent calling an LLM for itself —
the native tool-loop, the LangGraph nodes, the router's selection call.

complete for one-shot calls where the full response fits in memory, stream_complete for longer responses (e.g. tool-use traces) where we want to start processing before the full response arrives.

Resilience
----------
Every call is wrapped with `with_retry` (see `common/retry.py`):
exponential backoff + jitter on transient errors, plus a per-target
circuit breaker so a flapping model alias doesn't take everything down.
The OpenAI SDK's own retry loop is disabled (`max_retries=0`) so our
policy is the only one in play.

Tunables (env): LLM_TIMEOUT, LLM_MAX_RETRIES, LLM_RETRY_BASE_DELAY,
LLM_RETRY_MAX_DELAY, LLM_RETRY_JITTER, LLM_BREAKER_THRESHOLD,
LLM_BREAKER_RESET.

"""

from __future__ import annotations

from typing import Any, AsyncIterator

from openai import AsyncOpenAI

from config import get_settings
from .deadline import remaining_or
from .retry import with_retry

# How long the HTTP client waits before giving up on a single attempt.
# Retry/backoff sits on top of this.
DEFAULT_TIMEOUT = get_settings().llm_timeout


def make_async_client(*, timeout: float | None = None) -> AsyncOpenAI:
    """Return an AsyncOpenAI client wired to the LiteLLM proxy.

    `max_retries=0` so the SDK doesn't retry behind our back — our
    `with_retry` is the single source of truth for retry policy.
    """
    return AsyncOpenAI(
        base_url=get_settings().openai_compatible_base_url,
        api_key=get_settings().litellm_api_key,
        timeout=timeout if timeout is not None else DEFAULT_TIMEOUT,
        max_retries=0,
    )


def _build_kwargs(
    *,
    model: str,
    messages: list[dict],
    temperature: float | None,
    max_tokens: int | None,
    stream: bool,
) -> dict:
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": stream,
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    else:
        kwargs["max_tokens"] = 4096  # Default max_tokens if not specified, to prevent unbounded responses.
    return kwargs


async def complete(
    *,
    model: str,
    system: str,
    user: str,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: float | None = None,
) -> str:
    """One-shot system+user completion with retry + circuit breaker.

    Returns the assistant text.
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    async def _call() -> str:
        # Shrink to whichever is smaller: caller's request, env default,
        # or remaining time on the overall task deadline.
        eff_timeout = remaining_or(timeout if timeout is not None else DEFAULT_TIMEOUT)
        client = make_async_client(timeout=eff_timeout)
        resp = await client.chat.completions.create(
            **_build_kwargs(
                model=model, messages=messages,
                temperature=temperature, max_tokens=max_tokens, stream=False,
            ),
        )
        return (resp.choices[0].message.content or "").strip()

    return await with_retry(_call, target=model)


async def stream_complete(
    *,
    model: str,
    system: str,
    user: str,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: float | None = None,
) -> AsyncIterator[str]:
    """Stream completion deltas as they arrive.

    Retry policy covers the INITIAL stream-open call (the most common
    failure point). If the stream succeeds and then fails mid-flight,
    the error propagates to the caller — restarting a partially-consumed
    stream would silently duplicate output.
    """

    async def _open_stream():
        eff_timeout = remaining_or(timeout if timeout is not None else DEFAULT_TIMEOUT)
        client = make_async_client(timeout=eff_timeout)
        return await client.chat.completions.create(
            **_build_kwargs(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature, max_tokens=max_tokens, stream=True,
            ),
        )

    stream = await with_retry(_open_stream, target=model)

    async for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta
