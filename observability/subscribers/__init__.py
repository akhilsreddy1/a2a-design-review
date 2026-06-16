"""Event-bus subscribers — independent sinks fed by the central bus."""

from .console import register as register_console
from .jsonl import JsonlTraceWriter
from .sse import SSEConnection

__all__ = ["register_console", "JsonlTraceWriter", "SSEConnection"]
