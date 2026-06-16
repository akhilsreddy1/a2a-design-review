"""LiteLLMRegistry — agent discovery & registration via the LiteLLM gateway.

Endpoints (beta LiteLLM Agent Gateway):
  GET  /v1/agents
  POST /v1/agents                                  (409 → PUT update)
  GET  /a2a/{agent_id}/.well-known/agent-card.json
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from common.deadline import remaining_or
from common.decorations import get_decoration
from common.retry import with_retry
from config import get_settings
from models.a2a import AgentCardModel

logger = logging.getLogger("multi_agent.registry")


class LiteLLMRegistry:
    """LiteLLM agent registry adapter — discovery and registration."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.base_url = self.settings.litellm_base_url.rstrip("/")

    # ── http helpers ────────────────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        token = self.settings.litellm_api_key
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    @property
    def _list_timeout(self) -> float:
        return self.settings.litellm_hub_timeout

    @property
    def _register_timeout(self) -> float:
        return self.settings.litellm_register_timeout

    # ── discovery ───────────────────────────────────────────────────────────
    async def list_agents(self) -> list[dict[str, Any]]:
        """Raw registry rows from ``GET /v1/agents`` (shape-tolerant)."""

        async def _call() -> list[dict[str, Any]]:
            async with httpx.AsyncClient(timeout=remaining_or(self._list_timeout)) as client:
                resp = await client.get(f"{self.base_url}/v1/agents", headers=self._headers())
                resp.raise_for_status()
                payload = resp.json()
            if isinstance(payload, list):
                return payload
            if isinstance(payload, dict):
                for key in ("agents", "data", "results"):
                    if isinstance(payload.get(key), list):
                        return payload[key]
                if "name" in payload or "agent_name" in payload:
                    return [payload]
            return []

        return await with_retry(_call, target="registry:list_agents")

    async def discover_cards(self) -> list[AgentCardModel]:
        """Discover and normalize every registered agent into a typed card."""
        cards: list[AgentCardModel] = []
        for item in await self.list_agents():
            agent_id = (
                item.get("agent_id")
                or item.get("id")
                or item.get("agent_name")
                or item.get("name")
            )
            card_data = (
                item.get("agent_card_params")
                or item.get("agent_card")
                or item.get("card")
            )
            if not card_data and agent_id:
                try:
                    card_data = await self.get_agent_card(str(agent_id))
                except Exception as exc:
                    logger.warning("registry.card_fetch_failed agent=%s err=%s", agent_id, exc)
                    continue
            if not card_data:
                continue

            try:
                card = AgentCardModel.model_validate(card_data)
            except Exception as exc:
                logger.warning("registry.skip_invalid agent=%s err=%s", agent_id, exc)
                continue

            self._enrich_metadata(card, item, str(agent_id) if agent_id else card.name)
            cards.append(card)

        logger.info("registry.discover count=%d agents=%s", len(cards), [c.name for c in cards])
        return cards

    def _enrich_metadata(self, card: AgentCardModel, item: dict[str, Any], agent_id: str) -> None:
        """Layer LiteLLM litellm_params and local decorations into card.metadata."""
        litellm_params = item.get("litellm_params") or {}
        card.metadata.update(litellm_params.get("metadata") or {})
        for key in ("workflow_type", "model", "framework", "transport"):
            if key in litellm_params and litellm_params[key] is not None:
                card.metadata[key] = litellm_params[key]
        card.metadata.setdefault("agent_id", agent_id)

        # Local UI/routing decorations are keyed by agent NAME (e.g. "security"),
        # NOT the LiteLLM agent_id (a UUID). Win only where the registry was silent.
        deco = get_decoration(card.name)
        card.metadata.setdefault("role", deco.role)
        card.metadata.setdefault("icon", deco.icon)
        card.metadata.setdefault("color", deco.color)
        card.metadata.setdefault("model_alias", deco.model_alias)
        card.metadata.setdefault("expertise", list(deco.expertise))

    async def get_agent_card(self, agent_id: str) -> dict[str, Any]:
        """Fetch a single card from the gateway's well-known route."""

        async def _call() -> dict[str, Any]:
            async with httpx.AsyncClient(timeout=remaining_or(self._list_timeout)) as client:
                resp = await client.get(
                    f"{self.base_url}/a2a/{agent_id}/.well-known/agent-card.json",
                    headers=self._headers(),
                )
                resp.raise_for_status()
                return resp.json()

        return await with_retry(_call, target=f"registry:get_card:{agent_id}")

    # ── registration ────────────────────────────────────────────────────────
    async def register_agent(self, card: AgentCardModel) -> dict[str, Any]:
        """Register (or update on 409) an agent card with LiteLLM."""
        payload = self._agent_config_payload(card)
        logger.info("registry.register.start agent=%s url=%s", card.name, card.url)

        async with httpx.AsyncClient(timeout=self._register_timeout) as client:
            resp = await client.post(
                f"{self.base_url}/v1/agents", headers=self._headers(), json=payload
            )
            logger.info(
                "registry.register.response agent=%s status=%s body=%s",
                card.name, resp.status_code, resp.text[:500],
            )
            # Already exists → look up the id and PUT-update it.
            if resp.status_code in (400, 409) and "already exists" in resp.text.lower():
                agent_id = await self._find_agent_id(card.name)
                if not agent_id:
                    return {"status": "exists", "agent": card.name}
                upd = await client.put(
                    f"{self.base_url}/v1/agents/{agent_id}",
                    headers=self._headers(),
                    json=payload,
                )
                if upd.status_code >= 400:
                    return {"status": "exists", "agent": card.name, "update_status": upd.status_code}
                return {"status": "updated", "agent": card.name, "agent_id": agent_id}
            if resp.status_code == 422:
                raise RuntimeError(f"LiteLLM rejected {card.name}: {resp.text[:300]}")
            resp.raise_for_status()
            return {"status": "created", "agent": card.name, "response": resp.json()}

    async def _find_agent_id(self, agent_name: str) -> str | None:
        for item in await self.list_agents():
            if item.get("agent_name") == agent_name or item.get("name") == agent_name:
                return item.get("agent_id") or item.get("id")
        return None

    def _agent_config_payload(self, card: AgentCardModel) -> dict[str, Any]:
        return {
            "agent_name": card.name,
            "agent_card_params": self._agent_card_params(card),
            "litellm_params": {
                "url": str(card.url),
                "model": card.metadata.get("model") or card.metadata.get("model_alias"),
                "framework": card.metadata.get("framework"),
                "transport": card.metadata.get("transport", "a2a"),
                "metadata": card.metadata,
            },
        }

    def _agent_card_params(self, card: AgentCardModel) -> dict[str, Any]:
        return {
            "protocolVersion": "0.3.0",
            "name": card.name,
            "description": card.description,
            "url": str(card.url),
            "version": card.version,
            "capabilities": card.capabilities,
            "defaultInputModes": card.default_input_modes,
            "defaultOutputModes": card.default_output_modes,
            "skills": [s.model_dump(mode="json") for s in card.skills],
            "preferredTransport": "JSONRPC",
            "additionalInterfaces": [{"url": str(card.url), "transport": "JSONRPC"}],
        }

