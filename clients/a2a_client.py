"""Structured A2A client for LiteLLM gateway routes.

The client builds native A2A JSON-RPC ``message/send`` payloads,
propagates ``ExecutionContext`` as headers and metadata, and parses
task/status/artifact responses into typed envelopes.

"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

import httpx

from common.deadline import check_deadline, deadline_header_value, remaining_or
from common.retry import with_retry
from config import get_settings
from observability.context import ExecutionContext

logger = logging.getLogger("multi_agent.a2a_client")

A2AStreamKind = Literal["status", "artifact", "message", "token", "error", "raw"]


@dataclass(frozen=True)
class A2AResult:
    agent_name: str
    request_id: str
    task_id: str | None = None
    status: str | None = None
    content: str = ""
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None and self.status not in {"failed", "canceled", "rejected"}


@dataclass(frozen=True)
class A2AStreamEvent:
    agent_name: str
    kind: A2AStreamKind
    request_id: str
    task_id: str | None = None
    status: str | None = None
    text: str = ""
    phase: str | None = None   # status.message.metadata.phase (progress/working/…)
    data: dict[str, Any] = field(default_factory=dict)


class A2AClient:
    """Structured A2A client for agents registered in LiteLLM.

    Two transports, same structured surface (typed ``A2AResult`` /
    ``A2AStreamEvent``, ExecutionContext propagation):

    - ``transport="openai"`` — invoke via LiteLLM's
      OpenAI-compatible route (``/v1/chat/completions`` with
      ``model="a2a/<name>"``). This is LiteLLM's robust, supported path:
      LiteLLM does the A2A translation internally. The ExecutionContext
      rides in ``extra_body`` and on headers.
      
    - ``transport="jsonrpc"`` (default) — talk native A2A JSON-RPC
      directly to ``/v1/a2a/{name}/message/send`` on the LiteLLM gateway.
      Streaming uses the same endpoint (transport-level ``client.stream``),
      with automatic fallback to ``send()`` if the agent doesn't support it.

    The point of the structured client (vs. calling chat-completions
    directly from orchestration) is that the A2A boundary stays the
    orchestration API: orchestration says "invoke this agent with this
    task and this context" and gets a typed result back, regardless of
    which transport carries it.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
        route_prefix: str | None = None,
        transport: Literal["openai", "jsonrpc"] = "jsonrpc", # jsonrpc is default
    ) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.litellm_base_url).rstrip("/")
        self.api_key = api_key or settings.litellm_api_key
        self.timeout = timeout or settings.a2a_task_deadline
        self.route_prefix = "/" + (route_prefix or settings.a2a_gateway_route_prefix).strip("/")
        self.transport = transport

    def model_id(self, agent_name: str) -> str:
        """The OpenAI-compat model id for an A2A agent."""
        return f"a2a/{agent_name}"

    def openai_endpoint(self) -> str:
        return f"{self.base_url}/v1/chat/completions"

    def endpoint_for(self, agent_name: str, *, stream: bool = False) -> str:
        """A2A JSON-RPC endpoint.

        """
        if stream:
            return f"{self.base_url}{self.route_prefix}/{agent_name}/message/stream"
        return f"{self.base_url}{self.route_prefix}/{agent_name}/message/send"

    def headers(self, ctx: ExecutionContext | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if ctx is not None:
            headers.update(ctx.to_headers())
        dl = deadline_header_value()
        if dl is not None:
            headers["x-deadline-remaining"] = dl
        return headers

    def timeout_config(self, timeout: float | None = None) -> httpx.Timeout:
        effective = remaining_or(timeout if timeout is not None else self.timeout)
        return httpx.Timeout(timeout=effective, connect=10.0, read=effective, write=30.0, pool=10.0)


    def build_payload(
        self,
        *,
        text: str,
        ctx: ExecutionContext | None = None,
        metadata: dict[str, Any] | None = None,
        message_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Build a native A2A JSON-RPC ``message/send`` payload.

        The ExecutionContext rides in ``params.metadata.execution_context``
        (a structured channel) *and* on the HTTP headers (see :meth:`headers`),
        never inside the LLM-visible text — so correlation survives the hop
        without polluting the prompt.
        """
        params_metadata = dict(metadata or {})
        if ctx is not None:
            params_metadata["execution_context"] = ctx.model_dump(mode="json")
        dl = deadline_header_value()
        if dl is not None:
            params_metadata["deadline_remaining"] = float(dl)

        params: dict[str, Any] = {
            "message": {
                "role": "user",
                "messageId": message_id or str(uuid4()),
                "parts": [{"kind": "text", "text": text}],
            }
        }
        if params_metadata:
            params["metadata"] = params_metadata

        return {
            "jsonrpc": "2.0",
            "id": request_id or str(uuid4()),
            "method": "message/send",
            "params": params,
        }

    async def send(
        self,
        agent_name: str,
        text: str,
        *,
        ctx: ExecutionContext | None = None,
        metadata: dict[str, Any] | None = None,
        timeout: float | None = None,
        emit_events: bool = False,
    ) -> A2AResult:
        """Invoke an agent and return a structured result (transport-agnostic)."""
        if self.transport == "openai":
            return await self._send_openai(
                agent_name, text, ctx=ctx, metadata=metadata, timeout=timeout,
            )
        return await self._send_jsonrpc(
            agent_name, text, ctx=ctx, metadata=metadata, timeout=timeout,
        )

    async def _send_openai(
        self,
        agent_name: str,
        text: str,
        *,
        ctx: ExecutionContext | None = None,
        metadata: dict[str, Any] | None = None,
        timeout: float | None = None,
        emit_events: bool = False,
    ) -> A2AResult:
        """Invoke via LiteLLM's OpenAI-compatible A2A route (robust default)."""
        model = self.model_id(agent_name)
        request_id = str(uuid4())
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": text}],
        }
        extra_body: dict[str, Any] = dict(metadata or {})
        if ctx is not None:
            extra_body["execution_context"] = ctx.model_dump(mode="json")
        if extra_body:
            body["extra_body"] = extra_body

        async def _call() -> A2AResult:
            async with httpx.AsyncClient(timeout=self.timeout_config(timeout)) as client:
                resp = await client.post(self.openai_endpoint(), headers=self.headers(ctx), json=body)
                resp.raise_for_status()
                data = resp.json()
                content = ""
                try:
                    content = (data["choices"][0]["message"]["content"] or "").strip()
                except (KeyError, IndexError, TypeError):
                    content = self._extract_text(data)
                return A2AResult(
                    agent_name=agent_name,
                    request_id=request_id,
                    task_id=str(data.get("id") or "") or None,
                    status="completed",
                    content=content or "",
                    raw=data,
                    error=None,
                )

        return await with_retry(_call, target=model)

    async def _send_jsonrpc(
        self,
        agent_name: str,
        text: str,
        *,
        ctx: ExecutionContext | None = None,
        metadata: dict[str, Any] | None = None,
        timeout: float | None = None,
        emit_events: bool = False,
    ) -> A2AResult:
        request = self.build_payload(text=text, ctx=ctx, metadata=metadata)
        endpoint = self.endpoint_for(agent_name)
        target = f"a2a/{agent_name}"

        async def _call() -> A2AResult:
            async with httpx.AsyncClient(timeout=self.timeout_config(timeout)) as client:
                resp = await client.post(endpoint, headers=self.headers(ctx), json=request)
                resp.raise_for_status()
                return self.parse_response(agent_name, str(request["id"]), resp.json())

        return await with_retry(_call, target=target)

    async def stream(
        self,
        agent_name: str,
        text: str,
        *,
        ctx: ExecutionContext | None = None,
        metadata: dict[str, Any] | None = None,
        timeout: float | None = None,
        emit_events: bool = False,
    ) -> AsyncIterator[A2AStreamEvent]:
        """Stream from an agent, yielding structured events (transport-agnostic)."""
        if self.transport == "openai":
            async for ev in self._stream_openai(
                agent_name, text, ctx=ctx, metadata=metadata, timeout=timeout,
            ):
                yield ev
        else:
            async for ev in self._stream_jsonrpc(
                agent_name, text, ctx=ctx, metadata=metadata, timeout=timeout,
            ):
                yield ev

    async def stream_agent(
        self,
        agent_url: str,
        text: str,
        *,
        agent_name: str = "",
        ctx: ExecutionContext | None = None,
        metadata: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[A2AStreamEvent]:
        """Stream ``message/stream`` DIRECTLY from a native a2a-sdk agent.

        On any transport failure we fall back to a single blocking ``send()``
        through LiteLLM and yield the whole answer as one chunk — so callers
        always get the answer even if direct streaming isn't reachable.
        """
        check_deadline()
        request = self.build_payload(text=text, ctx=ctx, metadata=metadata)
        request["method"] = "message/stream"   # native a2a-sdk streams this
        request_id = str(request["id"])
        try:
            async with httpx.AsyncClient(timeout=self.timeout_config(timeout)) as client:
                async with client.stream(
                    "POST", agent_url, headers=self.headers(ctx), json=request,
                ) as resp:
                    if resp.status_code >= 400:
                        body = await resp.aread()
                        raise httpx.HTTPStatusError(
                            f"stream {resp.status_code}: {body.decode('utf-8', 'replace')[:200]}",
                            request=resp.request, response=resp,
                        )
                    async for line in resp.aiter_lines():
                        event = self.parse_stream_line(agent_name, request_id, line)
                        if event is not None:
                            yield event
        except (
            httpx.HTTPStatusError, httpx.ConnectError, httpx.RemoteProtocolError,
            httpx.ReadTimeout, httpx.ConnectTimeout,
        ) as exc:
            logger.warning("a2a.stream_agent.fallback agent=%s url=%s reason=%s", agent_name, agent_url, exc)
            result = await self._send_jsonrpc(agent_name, text, ctx=ctx, metadata=metadata, timeout=timeout)
            if result.content:
                yield A2AStreamEvent(
                    agent_name=agent_name, kind="token",
                    request_id=request_id, text=result.content,
                )

    async def _stream_openai(
        self,
        agent_name: str,
        text: str,
        *,
        ctx: ExecutionContext | None = None,
        metadata: dict[str, Any] | None = None,
        timeout: float | None = None,
        emit_events: bool = False,
    ) -> AsyncIterator[A2AStreamEvent]:
        """Stream via LiteLLM's OpenAI-compatible route (SSE chat chunks)."""
        check_deadline()
        model = self.model_id(agent_name)
        request_id = str(uuid4())
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": text}],
            "stream": True,
        }
        extra_body: dict[str, Any] = dict(metadata or {})
        if ctx is not None:
            extra_body["execution_context"] = ctx.model_dump(mode="json")
        if extra_body:
            body["extra_body"] = extra_body

        async with httpx.AsyncClient(timeout=self.timeout_config(timeout)) as client:
            async with client.stream("POST", self.openai_endpoint(), headers=self.headers(ctx), json=body) as resp:
                if resp.status_code >= 400:
                    resp.raise_for_status()
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[len("data:"):].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(raw)
                        delta = chunk["choices"][0]["delta"].get("content") or ""
                    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                        continue
                    if not delta:
                        continue
                    yield A2AStreamEvent(agent_name=agent_name, kind="token", request_id=request_id, text=delta)

    async def _stream_jsonrpc(
        self,
        agent_name: str,
        text: str,
        *,
        ctx: ExecutionContext | None = None,
        metadata: dict[str, Any] | None = None,
        timeout: float | None = None,
        emit_events: bool = False,
    ) -> AsyncIterator[A2AStreamEvent]:
        """Stream from a native A2A agent, falling back to ``send()`` on failure.

        Streaming is a transport-level concern: we POST to the same
        ``message/send`` endpoint but read the response incrementally via
        ``client.stream()``.  If the agent doesn't support streaming (404,
        connection error, protocol error), we fall back to a regular
        ``send()`` and yield the full content as one chunk — exactly the
        pattern from the reference ``agent-a2a`` client.
        """
        check_deadline()
        request = self.build_payload(text=text, ctx=ctx, metadata=metadata)
        request_id = str(request["id"])
        endpoint = self.endpoint_for(agent_name, stream=True)

        try:
            async with httpx.AsyncClient(timeout=self.timeout_config(timeout)) as client:
                async with client.stream("POST", endpoint, headers=self.headers(ctx), json=request) as resp:
                    if resp.status_code == 404:
                        raise httpx.HTTPStatusError(
                            "Streaming not supported by agent",
                            request=resp.request, response=resp,
                        )
                    if resp.status_code >= 400:
                        resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        event = self.parse_stream_line(agent_name, request_id, line)
                        if event is None:
                            continue
                        yield event
        except (httpx.HTTPStatusError, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            logger.warning("a2a.stream.fallback agent=%s reason=%s", agent_name, exc)
            result = await self._send_jsonrpc(
                agent_name, text, ctx=ctx, metadata=metadata, timeout=timeout,
            )
            if result.content:
                yield A2AStreamEvent(
                    agent_name=agent_name, kind="token",
                    request_id=request_id, text=result.content,
                )

    def parse_response(self, agent_name: str, request_id: str, payload: dict[str, Any]) -> A2AResult:
        """Parse JSON-RPC A2A response into a structured result."""
        if "error" in payload:
            return A2AResult(
                agent_name=agent_name,
                request_id=request_id,
                status="failed",
                error=self._extract_text(payload["error"]),
                raw=payload,
            )
        result = payload.get("result", payload)
        if not isinstance(result, dict):
            return A2AResult(
                agent_name=agent_name,
                request_id=request_id,
                content=self._extract_text(result),
                raw={"result": result},
            )
        artifacts = self._list_of_dicts(result.get("artifacts"))
        messages = self._list_of_dicts(result.get("messages"))
        status = self._status(result)
        return A2AResult(
            agent_name=agent_name,
            request_id=request_id,
            task_id=str(result.get("id") or result.get("taskId") or "") or None,
            status=status,
            content=self._extract_text(result),
            artifacts=artifacts,
            messages=messages,
            raw=result,
        )

    def parse_stream_line(
        self,
        agent_name: str,
        request_id: str,
        line: str,
    ) -> A2AStreamEvent | None:
        """Parse one SSE/data line into an A2AStreamEvent."""
        line = line.strip()
        if not line or line.startswith(":"):
            return None
        if line.startswith("event:"):
            return None
        raw = line.removeprefix("data:").strip()
        if not raw or raw == "[DONE]":
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return A2AStreamEvent(agent_name=agent_name, kind="raw", request_id=request_id, text=raw)
        if "error" in payload:
            return A2AStreamEvent(
                agent_name=agent_name,
                kind="error",
                request_id=request_id,
                text=self._extract_text(payload["error"]),
                data=payload,
            )
        result = payload.get("result", payload)
        if not isinstance(result, dict):
            return A2AStreamEvent(
                agent_name=agent_name,
                kind="token",
                request_id=request_id,
                text=self._extract_text(result),
                data={"result": result},
            )
        kind = self._stream_kind(result)
        return A2AStreamEvent(
            agent_name=agent_name,
            kind=kind,
            request_id=request_id,
            task_id=str(result.get("id") or result.get("taskId") or "") or None,
            status=self._status(result),
            text=self._extract_text(result),
            phase=self._phase(result),
            data=result,
        )

    def _phase(self, result: dict[str, Any]) -> str | None:
        """Extract the lifecycle phase the agent stamped on a status message.

        Agents tag answer-content deltas with phase="progress" and lifecycle
        chatter (working / consulting_peer / completed) with other phases, so
        the orchestrator can stream only the real answer.
        """
        status = result.get("status")
        if isinstance(status, dict):
            msg = status.get("message")
            if isinstance(msg, dict):
                meta = msg.get("metadata")
                if isinstance(meta, dict):
                    p = meta.get("phase")
                    return str(p) if p is not None else None
        return None

    def _stream_kind(self, result: dict[str, Any]) -> A2AStreamKind:
        if "status" in result:
            return "status"
        if "artifact" in result or "artifacts" in result:
            return "artifact"
        if "message" in result or "messages" in result:
            return "message"
        if self._extract_text(result):
            return "token"
        return "raw"

    def _status(self, value: dict[str, Any]) -> str | None:
        status = value.get("status")
        if isinstance(status, str):
            return status
        if isinstance(status, dict):
            state = status.get("state") or status.get("status")
            return str(state) if state is not None else None
        state = value.get("state")
        return str(state) if state is not None else None

    def _list_of_dicts(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

    def _extract_text(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "\n".join(text for item in value if (text := self._extract_text(item)))
        if isinstance(value, dict):
            if "text" in value:
                return str(value["text"])
            if "content" in value:
                return self._extract_text(value["content"])
            texts: list[str] = []
            # "artifact" (singular) is the ADK/a2a artifact-update shape;
            # without it the final answer in an artifact-update is lost.
            for key in ("parts", "artifact", "artifacts", "messages"):
                text = self._extract_text(value.get(key))
                if text:
                    texts.append(text)
            status = value.get("status")
            if isinstance(status, dict):
                text = self._extract_text(status.get("message"))
                if text:
                    texts.append(text)
            message = value.get("message")
            if isinstance(message, dict):
                text = self._extract_text(message)
                if text:
                    texts.append(text)
            return "\n".join(texts)
        return str(value)
