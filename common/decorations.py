"""UI/router decorations for each agent.

The A2A protobuf `AgentCard` is the canonical, wire-compatible identity for
an agent — but the spec has no slot for things like an emoji icon, a brand
color, an `expertise` keyword list for fallback routing, or a LiteLLM
`model_alias`. Those are local concerns.

This module holds that decoration data, keyed by agent name (= A2A card
name = LiteLLM agent_name = the value the router uses in `a2a/<name>`).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AgentDecoration:
    """Local UI + routing extras for one agent."""

    name: str                       # must match the protobuf AgentCard.name
    icon: str = "🤖"
    color: str = "#6366f1"
    role: str = "specialist"
    expertise: list[str] = field(default_factory=list)
    model_alias: str = "claude-opus-4-6"
    # Which agent framework backs this agent — surfaced in the registry +
    # the UI roster. One of: "a2a-sdk" | "langgraph" | "google-adk".
    framework: str = "a2a-sdk"
    # Other agents this one is allowed to consult mid-task (A2A peer-to-peer).
    # Empty list = this agent never reaches out to peers.
    peers: list[str] = field(default_factory=list)


DECORATIONS: dict[str, AgentDecoration] = {
    "developer": AgentDecoration(
        name="developer",
        icon="🧑‍💻",
        color="#3b82f6",
        role="Software Architect / Developer",
        model_alias="claude-opus-4-6",
        expertise=[
            "architecture", "system design", "api design", "data flow",
            "module", "service", "refactor", "implementation", "code",
            "build", "feature", "endpoint", "schema",
        ],
        peers=["security", "performance", "testing"],
    ),
    "security": AgentDecoration(
        name="security",
        icon="🛡️",
        color="#ef4444",
        role="Security Engineer",
        model_alias="claude-opus-4-6",
        framework="google-adk",
        expertise=[
            "security", "auth", "authentication", "authorization", "jwt", "oauth",
            "token", "secret", "credential", "pii", "pci", "gdpr", "compliance",
            "encryption", "tls", "ssl", "vulnerability", "xss", "csrf", "sqli",
            "injection", "threat", "owasp",
        ],
        peers=["developer"],
    ),
    "performance": AgentDecoration(
        name="performance",
        icon="⚡",
        color="#f59e0b",
        role="Performance Engineer",
        model_alias="claude-opus-4-6",
        framework="langgraph",
        expertise=[
            "performance", "latency", "throughput", "p95", "p99", "slow",
            "bottleneck", "profile", "profiling", "cache", "caching", "memory",
            "cpu", "gc", "garbage collection", "n+1", "query", "index",
            "load test", "stress test", "capacity", "scaling",
        ],
        peers=["developer", "devops"],
    ),
    "testing": AgentDecoration(
        name="testing",
        icon="🧪",
        color="#10b981",
        role="QA / Test Engineer",
        model_alias="claude-opus-4-6",
        expertise=[
            "test", "testing", "qa", "unit test", "integration test", "e2e",
            "regression", "coverage", "mock", "fixture", "test plan",
            "edge case", "property test", "fuzz", "snapshot",
        ],
        peers=["developer", "security"],
    ),
    "devops": AgentDecoration(
        name="devops",
        icon="🚀",
        color="#8b5cf6",
        role="DevOps / SRE",
        model_alias="claude-opus-4-6",
        expertise=[
            "deploy", "deployment", "ci", "cd", "pipeline", "docker", "kubernetes",
            "k8s", "helm", "terraform", "ansible", "infra", "infrastructure",
            "monitoring", "observability", "alert", "sre", "incident", "runbook",
            "rollback", "canary", "blue green", "github actions", "gitlab",
        ],
        peers=["security", "performance"],
    ),
    "code_reviewer": AgentDecoration(
        name="code_reviewer",
        icon="🔍",
        color="#0ea5e9",
        role="Senior Code Reviewer",
        model_alias="claude-opus-4-6",
        expertise=[
            "review", "code review", "pr", "pull request", "diff", "snippet",
            "lint", "style", "best practice", "smell", "refactor suggestion",
            "bug", "regression", "code quality",
        ],
        peers=["security", "performance", "testing"],
    ),
}


def get_decoration(name: str) -> AgentDecoration:
    """Look up by agent name. Returns a default if not registered."""
    return DECORATIONS.get(name, AgentDecoration(name=name))
