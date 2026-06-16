"""Definitions of every specialist agent (a2a-sdk 0.3.x).

Each specialist is a `SpecialistSpec` that yields:
  - the Pydantic A2A `AgentCard` to advertise
  - the system prompt that defines its persona and output structure

UI-only metadata (icon, color, expertise keywords, LiteLLM model alias)
lives in `common/decorations.py` — the A2A AgentCard schema has no slot
for those.
"""

from __future__ import annotations

from dataclasses import dataclass

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    TransportProtocol,
)

from config import get_settings
from common.decorations import DECORATIONS, get_decoration


@dataclass(frozen=True)
class SpecialistSpec:
    """Everything needed to launch one specialist agent."""

    id: str
    default_port: int
    system_prompt: str
    skills: list[AgentSkill]
    description: str

    def resolve_port(self) -> int:
        try:
            return get_settings().port_for(self.id)
        except KeyError:
            return self.default_port

    def model_alias(self) -> str:
        return get_decoration(self.id).model_alias

    def base_url(self) -> str:
        return get_settings().public_agent_url(self.resolve_port())

    def build_card(self) -> AgentCard:
        base = self.base_url()
        rpc_url = f"{base}/a2a"
        return AgentCard(
            name=self.id,
            description=self.description,
            version="1.0.0",
            url=rpc_url,
            preferred_transport=TransportProtocol.jsonrpc.value,
            protocol_version="0.3.0",
            documentation_url="https://a2a-protocol.org/latest/specification/",
            provider=AgentProvider(
                organization="Multi-Agent A2A Demo",
                url="https://github.com/",
            ),
            additional_interfaces=[
                AgentInterface(
                    url=rpc_url,
                    transport=TransportProtocol.jsonrpc.value,
                ),
            ],
            capabilities=AgentCapabilities(
                streaming=True,
                push_notifications=False,
            ),
            default_input_modes=["text"],
            default_output_modes=["text"],
            skills=self.skills,
        )


# ---------------------------------------------------------------------------
#  Skills (Pydantic AgentSkill objects)
# ---------------------------------------------------------------------------


def _skill(*, id: str, name: str, description: str, tags: list[str], examples: list[str]) -> AgentSkill:
    return AgentSkill(
        id=id,
        name=name,
        description=description,
        tags=tags,
        examples=examples,
        input_modes=["text"],
        output_modes=["text"],
    )


_DEV_SKILLS = [
    _skill(
        id="system-design",
        name="System Design",
        description="Architecture overviews, component breakdowns, and data flows.",
        tags=["architecture", "design"],
        examples=[
            "Design a multi-tenant billing service.",
            "Sketch the module layout for an event ingestion pipeline.",
        ],
    ),
    _skill(
        id="implementation-plan",
        name="Implementation Plan",
        description="Turns a design into concrete build steps and pseudocode.",
        tags=["coding", "planning"],
        examples=[
            "Plan the FastAPI endpoints for a JWT auth service.",
            "Write a code skeleton for a Kafka consumer worker.",
        ],
    ),
]

_SEC_SKILLS = [
    _skill(
        id="threat-model",
        name="Threat Modeling",
        description="STRIDE-style threat enumeration and risk ranking.",
        tags=["security", "threat-model"],
        examples=[
            "Threat-model a public REST API behind an API gateway.",
            "Identify risks in storing OAuth refresh tokens at rest.",
        ],
    ),
    _skill(
        id="mitigations",
        name="Mitigations & Hardening",
        description="Concrete defenses and a hardening checklist.",
        tags=["security", "hardening"],
        examples=[
            "Harden a JWT auth flow against token replay.",
            "Recommend mitigations for an XSS finding in a React app.",
        ],
    ),
]

_PERF_SKILLS = [
    _skill(
        id="bottleneck-diagnosis",
        name="Bottleneck Diagnosis",
        description="Walks through symptoms → hypotheses → measurements → fixes.",
        tags=["performance", "diagnosis"],
        examples=[
            "API p99 jumped from 200ms to 2s — diagnose it.",
            "Find the bottleneck in this batch ETL job.",
        ],
    ),
    _skill(
        id="capacity-plan",
        name="Capacity Planning",
        description="Sizes infra for target QPS and latency budgets.",
        tags=["performance", "capacity"],
        examples=[
            "Size a Redis cluster for 50k QPS with 1ms p99.",
            "Capacity-plan a video transcoding pipeline.",
        ],
    ),
]

_TEST_SKILLS = [
    _skill(
        id="test-plan",
        name="Test Plan",
        description="Unit / integration / e2e plan with explicit edge cases.",
        tags=["testing", "planning"],
        examples=[
            "Write a test plan for a Stripe webhook handler.",
            "Plan coverage for a distributed lock implementation.",
        ],
    ),
    _skill(
        id="edge-cases",
        name="Edge Cases & Failure Modes",
        description="Enumerates failure modes and validation checks.",
        tags=["testing", "edge-cases"],
        examples=[
            "List edge cases for a date-range picker.",
            "What failure modes should I test for a retry-with-backoff client?",
        ],
    ),
]

_DEVOPS_SKILLS = [
    _skill(
        id="deployment-strategy",
        name="Deployment Strategy",
        description="CI/CD pipeline design, rollout, rollback, and feature flags.",
        tags=["devops", "deployment"],
        examples=[
            "Design a blue/green deploy for a FastAPI service on EKS.",
            "Set up a canary rollout with automatic rollback.",
        ],
    ),
    _skill(
        id="observability",
        name="Observability",
        description="Metrics, logs, traces, and SLOs.",
        tags=["devops", "observability"],
        examples=[
            "What SLOs should I set for a checkout API?",
            "Design alerts for a Kafka consumer lag.",
        ],
    ),
]

_CR_SKILLS = [
    _skill(
        id="pr-review",
        name="PR Review",
        description="Reviews a diff with severity-ranked comments.",
        tags=["review", "pr"],
        examples=[
            "Review this PR for the new login endpoint.",
            "What's wrong with this snippet?",
        ],
    ),
]


# ---------------------------------------------------------------------------
#  System prompts
# ---------------------------------------------------------------------------


_DEVELOPER_PROMPT = """You are the **Developer Agent** in a multi-agent A2A team.

You handle architecture and implementation questions. When the question
materially needs security, performance, or testing input, consult those
peer agents via the `consult_peer` tool (see the Peer consultation
section below) and weave their input into your design.

Always answer in this structure (use markdown):

### Summary
One paragraph: what you're going to build and why.

### Architecture
Components and how they connect. ASCII diagram if helpful.

### Key Interfaces
Endpoints, function signatures, message shapes — concrete.

### Implementation Steps
Numbered list of build steps, smallest viable scope first.

### Tradeoffs & Open Questions
What you chose and what alternatives exist; what's still ambiguous.
"""

_SECURITY_PROMPT = """You are the **Security Agent** in a multi-agent A2A team.

You answer through a security lens: threats, controls, and verification.
Be specific — name the threat class (e.g. STRIDE category, OWASP item)
and the concrete mitigation. Avoid generic advice.

Always answer in this structure (use markdown):

### Threat Model
Trust boundaries, assets at risk, and primary attack surfaces.

### Risks (ranked)
A prioritized list. Each item: **risk** — likelihood/impact — short rationale.

### Mitigations
For each risk, the specific control. Cite standards (OWASP, NIST) when relevant.

### Hardening Checklist
A short bullet list a reviewer can tick through.
"""

_PERFORMANCE_PROMPT = """You are the **Performance Agent** in a multi-agent A2A team.

You think in symptom → hypothesis → measurement → fix. You give numbers
whenever you can: latency budgets, throughput targets, memory ceilings.

Always answer in this structure (use markdown):

### Hypotheses
What likely causes the symptom, ranked by probability.

### Measurements to Run
Exact tools and metrics: `perf`, `py-spy`, `EXPLAIN ANALYZE`, p95/p99, etc.

### Recommended Fixes
Concrete code/config changes, ordered by ROI.

### Expected Impact
Quantify: "should cut p99 from ~X to ~Y".
"""

_TESTING_PROMPT = """You are the **Testing Agent** in a multi-agent A2A team.

You design pragmatic test strategies — not theoretical. You name the test
framework when relevant and write a sample test case or two.

Always answer in this structure (use markdown):

### Test Strategy
What test types to invest in for this feature and why.

### Unit Tests
What to cover, with at least one sample test.

### Integration / E2E Tests
Critical paths and how to exercise them.

### Edge Cases & Failure Modes
A bullet list — be exhaustive here.

### Observability Checks
What to assert in logs / metrics to catch regressions in prod.
"""

_DEVOPS_PROMPT = """You are the **DevOps / SRE Agent** in a multi-agent A2A team.

You handle deployment, CI/CD, infra-as-code, observability, and incident
response. Give concrete tools and short config snippets — not just principles.

Always answer in this structure (use markdown):

### Goal
Restate the deploy/infra goal in one line.

### Pipeline / Topology
Stages, environments, and where the change flows.

### Configuration
Short YAML / HCL / shell snippet that captures the key bits.

### Rollout & Rollback
Explicit canary/blue-green plan and rollback trigger.

### Observability & Alerts
SLOs, dashboards, and the alerts that would page someone.
"""

_CODE_REVIEWER_PROMPT = """You are the **Code Reviewer Agent** in a multi-agent A2A team.

You review code with the eye of a senior engineer. Severity matters more
than count. Be direct, cite line/function names when possible, and offer
the corrected version of the code when the fix is small.

Always answer in this structure (use markdown):

### Verdict
**approve** / **request-changes** / **comment-only** — plus one line of why.

### Blocking Issues
Must-fix bugs, security holes, or correctness gaps. Quote the offending code.

### Suggestions
Non-blocking improvements — style, naming, structure.

### Nits
Tiny things. Group into one bullet list.
"""


# ---------------------------------------------------------------------------
#  Registry of all specialists
# ---------------------------------------------------------------------------

SPECIALISTS: dict[str, SpecialistSpec] = {
    "developer": SpecialistSpec(
        id="developer",
        default_port=9101,
        system_prompt=_DEVELOPER_PROMPT,
        skills=_DEV_SKILLS,
        description=(
            "Designs systems and writes implementation plans. Best for architecture, "
            "module boundaries, data flow, API design, and code skeletons."
        ),
    ),
    "security": SpecialistSpec(
        id="security",
        default_port=9102,
        system_prompt=_SECURITY_PROMPT,
        skills=_SEC_SKILLS,
        description=(
            "Threat-models systems and recommends mitigations. Best for auth, "
            "secrets, PII, compliance, and vulnerability triage."
        ),
    ),
    "performance": SpecialistSpec(
        id="performance",
        default_port=9103,
        system_prompt=_PERFORMANCE_PROMPT,
        skills=_PERF_SKILLS,
        description=(
            "Diagnoses and tunes performance. Best for latency, throughput, "
            "caching, database tuning, and capacity planning."
        ),
    ),
    "testing": SpecialistSpec(
        id="testing",
        default_port=9104,
        system_prompt=_TESTING_PROMPT,
        skills=_TEST_SKILLS,
        description=(
            "Designs test strategies. Best for unit/integration/e2e plans, "
            "edge cases, and observability checks."
        ),
    ),
    "devops": SpecialistSpec(
        id="devops",
        default_port=9105,
        system_prompt=_DEVOPS_PROMPT,
        skills=_DEVOPS_SKILLS,
        description=(
            "Handles deployment, CI/CD, infra-as-code, observability, and incidents."
        ),
    ),
    "code_reviewer": SpecialistSpec(
        id="code_reviewer",
        default_port=9106,
        system_prompt=_CODE_REVIEWER_PROMPT,
        skills=_CR_SKILLS,
        description=(
            "Reviews diffs and snippets for correctness, security, performance, "
            "and maintainability. Returns prioritized comments."
        ),
    ),
}


def get_spec(agent_id: str) -> SpecialistSpec:
    if agent_id not in SPECIALISTS:
        raise KeyError(f"Unknown specialist `{agent_id}`. Known: {sorted(SPECIALISTS)}")
    return SPECIALISTS[agent_id]


__all__ = ["SpecialistSpec", "SPECIALISTS", "get_spec", "DECORATIONS"]
