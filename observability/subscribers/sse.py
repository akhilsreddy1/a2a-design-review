"""SSE subscriber — bridges the event bus to the React UI's EventSource.

Each connected client gets a conversation-scoped subscription feeding a
bounded ``asyncio.Queue``. The HTTP handler drains that queue and writes
SSE frames. The SSE layer subscribes to the bus and never touches the
filesystem — the bus is the only runtime channel.

The subscription is created eagerly in ``SSEConnection.__init__`` (not on
first iteration), so a client can open the stream and *then* trigger a run
without racing past the first events.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from observability.event_bus import event_bus
from observability.events import BaseEvent

logger = logging.getLogger("multi_agent.sse")

# Cap per-connection buffering; drop oldest on overflow so one slow client
# can never stall the bus or the producer.
_MAX_QUEUE = 1000


class SSEConnection:
    """One client connection, scoped to a single conversation."""

    def __init__(self, conversation_id: str) -> None:
        self.conversation_id = conversation_id
        self._queue: asyncio.Queue[BaseEvent] = asyncio.Queue(maxsize=_MAX_QUEUE)
        self._subscription = event_bus.subscribe(
            self._on_event, conversation_id=conversation_id
        )
        logger.info(
            "sse.subscribe conversation_id=%s subscribers=%s",
            conversation_id, event_bus.subscriber_count,
        )

    def _on_event(self, event: BaseEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop oldest — liveness over completeness for a UI stream.
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(event)
            except asyncio.QueueEmpty:
                pass

    async def events(self) -> AsyncIterator[BaseEvent]:
        try:
            while True:
                yield await self._queue.get()
        finally:
            self.close()

    def close(self) -> None:
        self._subscription.unsubscribe()
        logger.info("sse.unsubscribe conversation_id=%s", self.conversation_id)
