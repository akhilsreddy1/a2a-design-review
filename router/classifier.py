"""Router / classifier — picks the right specialist for a query.

Discovery: ask LiteLLM's AI Hub which agents are registered.
Decision:  ask the LLM (through the same LiteLLM gateway) to choose from
           that list. Fall back to keyword overlap, then a deterministic
           default.

Output is always the same:
    RouteDecision(agent_id, confidence, reason, method, scores)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from config import get_settings
from common.llm_client import complete

logger = logging.getLogger("multi_agent.router")


_ROUTER_SYSTEM = """You are a router for a multi-agent A2A team.

You will receive:
  - A user question.
  - A list of available specialist agents with their roles, descriptions,
    and skill examples.

Choose the SINGLE best agent to handle the question. If multiple could
plausibly answer, prefer the one whose role is the primary lens — for
example, an "auth token storage" question is `security`, even if a
developer could also answer.

Reply with STRICT JSON only, no prose, in this shape:
{
  "agent_id": "<one of the provided agent_ids>",
  "confidence": <float 0..1>,
  "reason": "<one short sentence>"
}
"""


@dataclass
class RouteDecision:
    agent_id: str
    confidence: float
    reason: str
    method: str
    scores: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "confidence": self.confidence,
            "reason": self.reason,
            "method": self.method,
            "scores": self.scores,
        }


def _agents_block(agents: list[dict[str, Any]]) -> str:
    """Render the candidate agents into the router prompt."""
    lines: list[str] = []
    for card in agents:
        skill_examples: list[str] = []
        for skill in card.get("skills", [])[:3]:
            ex = skill.get("examples") or []
            if ex:
                skill_examples.append(f"  - {skill.get('name','?')}: {ex[0]}")
        expertise = ", ".join(card.get("expertise", [])[:12]) if card.get("expertise") else ""
        lines.append(
            "- id: {id}\n  role: {role}\n  description: {desc}{exp}{ex}".format(
                id=card["id"],
                role=card.get("role", "specialist"),
                desc=card.get("description", ""),
                exp=f"\n  expertise: {expertise}" if expertise else "",
                ex=("\n  example tasks:\n" + "\n".join(skill_examples)) if skill_examples else "",
            )
        )
    return "\n".join(lines)


def _keyword_scores(query: str, agents: list[dict[str, Any]]) -> dict[str, float]:
    """Cheap fallback: count expertise- and skill-tag matches in the query."""
    text_lower = query.lower()
    tokens = set(re.findall(r"[a-zA-Z0-9+#]+", text_lower))
    scores: dict[str, float] = {}
    for card in agents:
        keywords: list[str] = []
        keywords.extend(card.get("expertise", []))
        for skill in card.get("skills", []):
            keywords.extend(skill.get("tags", []))
        hit = 0.0
        for kw in keywords:
            k = kw.lower()
            if " " in k:
                if k in text_lower:
                    hit += 2
            elif k in tokens:
                hit += 1
        scores[card["id"]] = hit
    return scores


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


async def route(
    query: str,
    *,
    agents: list[dict[str, Any]],
    router_model: str | None = None,
) -> RouteDecision:
    """Pick the best agent for `query` among `agents` (from LiteLLM agent_hub)."""

    """ Routing logic:
    - LLM routing through LiteLLM
    - Keyword scoring over expertise tags if LLM fails
    - Default to developer agent if no strong signal

    """

    if not agents:
        raise ValueError("No agents available to route to.")

    keyword_scores = _keyword_scores(query, agents)
    model = router_model or get_settings().router_model

    # --- 1. Try LLM routing through LiteLLM ---------------------------------
    try:
        prompt = (
            f"User question:\n{query.strip()}\n\n"
            f"Available agents:\n{_agents_block(agents)}\n\n"
            f"Pick exactly one agent_id from the list above."
        )
        raw = await complete(
            model=model,
            system=_ROUTER_SYSTEM,
            user=prompt,
        )
        cleaned = _strip_code_fence(raw)
        decision = json.loads(cleaned)
        chosen = str(decision.get("agent_id", "")).strip()
        known_ids = {a["id"] for a in agents}
        if chosen in known_ids:
            return RouteDecision(
                agent_id=chosen,
                confidence=float(decision.get("confidence", 0.7)),
                reason=str(decision.get("reason", "LLM routing through LiteLLM gateway")).strip(),
                method="llm",
                scores=keyword_scores,
            )
        logger.warning("router.unknown_agent agent_id=%s fallback=keyword", chosen)
    except Exception as exc:
        logger.warning("router.llm_failed fallback=keyword err=%s", exc)

    # --- 2. Keyword fallback ------------------------------------------------
    if keyword_scores and max(keyword_scores.values()) > 0:
        best_id = max(keyword_scores, key=keyword_scores.get)
        total = sum(keyword_scores.values()) or 1.0
        confidence = min(0.9, keyword_scores[best_id] / total)
        return RouteDecision(
            agent_id=best_id,
            confidence=confidence,
            reason="Picked by keyword overlap with the agent's expertise / skill tags.",
            method="keyword",
            scores=keyword_scores,
        )

    # --- 3. Default ---------------------------------------------------------
    default_id = "developer" if any(a["id"] == "developer" for a in agents) else agents[0]["id"]
    return RouteDecision(
        agent_id=default_id,
        confidence=0.3,
        reason="No strong signal — defaulting to the developer agent.",
        method="default",
        scores=keyword_scores,
    )
