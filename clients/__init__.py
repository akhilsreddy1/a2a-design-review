"""Client boundaries for LiteLLM Agent Gateway calls.

A2A invocation (orchestrator → agent, and agent → peer) goes through
:class:`A2AClient`. An agent's OWN LLM reasoning uses the helpers in
`common.llm_client`.
"""

from .a2a_client import A2AClient, A2AResult, A2AStreamEvent

__all__ = ["A2AClient", "A2AResult", "A2AStreamEvent"]
