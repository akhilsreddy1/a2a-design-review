"""Register every specialist agent with the LiteLLM Agent Gateway.

Builds a typed :class:`models.a2a.AgentCardModel` from each ``SpecialistSpec``
(+ its local decoration) and registers it via
:meth:`registry.LiteLLMRegistry.register_agent`, which does an idempotent
upsert: ``POST /v1/agents`` and, on "already exists", a ``PUT`` update. So
re-running this is clean — no more "already exists" failures.

After registration each agent is reachable via:
  - POST /v1/chat/completions  model="a2a/{agent_name}"   (OpenAI-compatible)
  - POST /a2a/{agent_name}/message/send                    (native A2A JSON-RPC)

Discover them via:  GET /v1/agents
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.specialists import SPECIALISTS  # noqa: E402
from common.decorations import get_decoration  # noqa: E402
from models.a2a import AgentCardModel, AgentSkillModel  # noqa: E402
from registry import LiteLLMRegistry  # noqa: E402

from common.log import setup as _setup_logging
_setup_logging()
logger = logging.getLogger("multi_agent.register")


def _spec_to_card(spec) -> AgentCardModel:
    """Build a typed registry card from a SpecialistSpec + its decoration."""
    deco = get_decoration(spec.id)
    return AgentCardModel(
        name=spec.id,
        description=spec.description,
        url=f"{spec.base_url()}/a2a",
        version="1.0.0",
        capabilities={"streaming": True, "pushNotifications": False},
        skills=[
            AgentSkillModel(
                id=s.id,
                name=s.name,
                description=s.description,
                tags=list(s.tags or []),
                examples=list(s.examples or []),
            )
            for s in spec.skills
        ],
        metadata={
            "framework": deco.framework,
            "model": deco.model_alias,
            "model_alias": deco.model_alias,
            "role": deco.role,
            "icon": deco.icon,
            "color": deco.color,
            "expertise": list(deco.expertise),
            "transport": "a2a",
        },
    )


async def register_all() -> int:
    registry = LiteLLMRegistry()
    logger.info("register.start count=%d gateway=%s", len(SPECIALISTS), registry.base_url)

    failures = 0
    for spec in SPECIALISTS.values():
        card = _spec_to_card(spec)
        try:
            result = await registry.register_agent(card)
            status = result.get("status", "ok")
            logger.info("register.ok agent=%s url=%s status=%s", card.name, card.url, status)
        except Exception as exc:
            failures += 1
            logger.warning("register.failed agent=%s url=%s err=%s", card.name, card.url, str(exc)[:200])

    if failures:
        logger.warning("register.incomplete failures=%d", failures)
    else:
        logger.info("register.complete count=%d", len(SPECIALISTS))
    return 0 if failures == 0 else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(register_all()))
