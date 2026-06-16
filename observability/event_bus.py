"""Central in-process asyncio event bus.

One bus, many subscribers, with fan-out. Subscriptions can be scoped to
a ``conversation_id`` so a subscriber (e.g. one SSE connection) only
sees events for its own conversation — this is how multi-user isolation
works without spinning up a bus per user.

The public surface is intentionally tiny — ``subscribe`` / ``publish`` /
``Subscription.unsubscribe`` — so this in-memory implementation can be
swapped for Redis Streams / NATS / Kafka later without touching
producers or subscribers (the seam matters more than the transport).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .events import BaseEvent

logger = logging.getLogger("multi_agent.bus")

# A subscriber callback. Sync or async; both are supported.
Callback = Callable[["BaseEvent"], "Awaitable[None] | None"]


@dataclass
class Subscription:
    callback: Callback
    conversation_id: str | None  # None = receive ALL conversations
    _bus: "EventBus" = field(repr=False)
    active: bool = True

    def unsubscribe(self) -> None:
        self.active = False
        self._bus._remove(self)


class EventBus:
    """Async pub/sub with conversation-scoped fan-out and error isolation."""

    def __init__(self) -> None:
        self._subscriptions: list[Subscription] = []

    def subscribe(
        self,
        callback: Callback,
        *,
        conversation_id: str | None = None,
    ) -> Subscription:
        """Register a subscriber. Scope to a conversation_id, or None for all."""
        sub = Subscription(
            callback=callback,
            conversation_id=conversation_id,
            _bus=self,
        )
        self._subscriptions.append(sub)
        return sub

    def _remove(self, sub: Subscription) -> None:
        try:
            self._subscriptions.remove(sub)
        except ValueError:
            pass

    async def publish(self, event: "BaseEvent") -> None:
        """Fan out an event to all matching subscribers.

        Per-subscriber error isolation: one slow or failing subscriber never
        breaks the others or the producer. Subscribers that need true
        decoupling (SSE) should hand off to their own queue immediately.
        """
        # Snapshot subscribers so add/remove during iteration is safe.
        targets = [
            sub
            for sub in list(self._subscriptions)
            if sub.active
            and (sub.conversation_id is None or sub.conversation_id == event.conversation_id)
        ]
        for sub in targets:
            try:
                result = sub.callback(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception(
                    "subscriber.error event_type=%s conversation_id=%s",
                    event.event_type, event.conversation_id,
                )

    @property
    def subscriber_count(self) -> int:
        return len(self._subscriptions)


# The single global bus (one process). Multi-user isolation is by subscription
# scope, NOT by multiple buses.
event_bus = EventBus()
