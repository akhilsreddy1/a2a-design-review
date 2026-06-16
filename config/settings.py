"""Canonical settings for the multi-agent A2A demo.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=DEFAULT_ENV_PATH,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "local"

    # --- LiteLLM gateway ------------------------------------------------------
    litellm_base_url: str = "http://localhost:4000"
    litellm_api_key: str = ""

    # --- LLM model defaults ---------------------------------------------------
    default_model: str = "router-model"
    router_model: str = "router-model"

    # --- Agent host / ports ---------------------------------------------------
    agent_host: str = "127.0.0.1"
    agent_public_host: str = "localhost"
    developer_port: int = 9101
    security_port: int = 9102
    performance_port: int = 9103
    testing_port: int = 9104
    devops_port: int = 9105
    code_reviewer_port: int = 9106

    # --- Bridge ---------------------------------------------------------------
    bridge_url: str = "http://localhost:8080"

    # --- Resilience -----------------------------------------------------------
    a2a_task_deadline: float = 180.0
    llm_timeout: float = 60.0
    llm_max_retries: int = Field(default=3, ge=1)
    llm_retry_base_delay: float = 0.5
    llm_retry_max_delay: float = 8.0
    llm_retry_jitter: bool = True
    llm_breaker_threshold: int = Field(default=5, ge=1)
    llm_breaker_reset: float = 30.0

    # --- LiteLLM hub / registry timeouts --------------------------------------
    litellm_hub_timeout: float = 10.0
    litellm_register_timeout: float = 15.0
    a2a_gateway_route_prefix: str = "/v1/a2a"

    # --- Derived helpers ------------------------------------------------------
    @property
    def openai_compatible_base_url(self) -> str:
        return f"{self.litellm_base_url.rstrip('/')}/v1"

    def public_agent_url(self, port: int) -> str:
        return f"http://{self.agent_public_host}:{port}"

    def port_for(self, agent_id: str) -> int:
        ports = {
            "developer": self.developer_port,
            "security": self.security_port,
            "performance": self.performance_port,
            "testing": self.testing_port,
            "devops": self.devops_port,
            "code_reviewer": self.code_reviewer_port,
        }
        try:
            return ports[agent_id]
        except KeyError as exc:
            raise KeyError(f"Unknown specialist `{agent_id}`. Known: {sorted(ports)}") from exc


@lru_cache
def get_settings() -> Settings:
    return Settings()
