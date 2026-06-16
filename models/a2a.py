from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from pydantic import BaseModel, Field


class AgentSkillModel(BaseModel):
    id: str = ""
    name: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)


class AgentCardModel(BaseModel):
    """Normalized agent card, as discovered from the LiteLLM registry.

    ``metadata`` is the flexible bag where we stash framework, role, model
    alias, icon, color, expertise, and the LiteLLM ``agent_id`` — none of
    which the strict A2A card schema has a slot for.
    """

    name: str
    description: str = ""
    url: str = ""
    version: str = "1.0.0"
    capabilities: dict[str, Any] = Field(default_factory=dict)
    skills: list[AgentSkillModel] = Field(default_factory=list)
    default_input_modes: list[str] = Field(default_factory=lambda: ["text"])
    default_output_modes: list[str] = Field(default_factory=lambda: ["text"])
    metadata: dict[str, Any] = Field(default_factory=dict)

    # convenience accessors used by the router/UI
    @property
    def agent_id(self) -> str:
        return str(self.metadata.get("agent_id") or self.name)

    @property
    def framework(self) -> str | None:
        return self.metadata.get("framework")

    @property
    def role(self) -> str | None:
        return self.metadata.get("role")


class AgentResult(BaseModel):
    agent_name: str
    task_id: str
    status: Literal["completed", "failed", "partial"] = "completed"
    content: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    latency_ms: float = 0
    model: str | None = None
    error: str | None = None


class OrchestrationResult(BaseModel):
    """The envelope returned by a full router run.

    Mirrors the reference's trace/result envelope, plus the routing decision
    so a non-streaming caller still gets the full picture.
    """

    run_id: str
    conversation_id: str
    trace_id: str
    selected_agent: str
    final_answer: str = ""
    method: str | None = None        # llm / keyword / pinned / default
    confidence: float | None = None
    reason: str | None = None
    latency_ms: float = 0
    agent_results: list[AgentResult] = Field(default_factory=list)
    discovered_agents: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
