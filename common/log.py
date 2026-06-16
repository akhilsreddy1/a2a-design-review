"""Centralized logging setup — called once per process entry point.

Every module uses ``logging.getLogger("multi_agent.<component>")`` for its
logger.  Each process entry point (agent servers, bridge, scripts) calls
:func:`setup` once at startup to install the shared format + level.

Structured message convention (grep-friendly, consistent):
    component.action key=value key=value ...

    Examples:
        registry.discover count=6 agents=[developer, security, ...]
        orchestration.invoke_failed trace_id=abc123 agent=developer
        peer.consult.start peer=security question_chars=120
"""

from __future__ import annotations

import logging

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATEFMT = "%H:%M:%S"


def setup(level: int = logging.INFO) -> None:
    """Configure root logger once. Safe to call multiple times."""
    logging.basicConfig(
        level=level,
        format=LOG_FORMAT,
        datefmt=LOG_DATEFMT,
        force=False,
    )
