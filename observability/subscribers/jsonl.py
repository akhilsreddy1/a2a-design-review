"""JSONL persistence subscriber — durable, replayable trace history.

One append-only file per trace:
    .data/traces/{conversation_id}/{trace_id}.jsonl

Each line is one serialized event. This is *persistence only* — it is never
read back as the runtime communication channel (the bus is). It exists for
audit, debugging, and replay.

Flow:
    producer → event_bus.publish(event)
      → fan-out to JsonlTraceWriter._safe_write
        → append json.dumps(event.to_json()) to the per-trace file
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from observability.event_bus import Subscription, event_bus
from observability.events import BaseEvent

logger = logging.getLogger("multi_agent.jsonl")

# Root for trace files. Override with a2a_trace_dir.
_DEFAULT_ROOT = os.getenv("a2a_trace_dir") or os.getenv("A2A_TRACE_DIR", ".data/traces")


class JsonlTraceWriter:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or _DEFAULT_ROOT)

    def _path(self, event: BaseEvent) -> Path:
        return self.root / event.conversation_id / f"{event.trace_id}.jsonl"

    def write(self, event: BaseEvent) -> None:
        path = self._path(event)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event.to_json()) + "\n")

    def register(self) -> Subscription:
        """Subscribe to all conversations (persistence is global)."""
        return event_bus.subscribe(self._safe_write)

    def _safe_write(self, event: BaseEvent) -> None:
        try:
            self.write(event)
        except Exception:
            logger.exception("jsonl.write_failed trace_id=%s", event.trace_id)
