"""Environment helpers — thin wrappers around centralised settings."""

from __future__ import annotations

from config.settings import get_settings


def get_litellm_base_url() -> str:
    return get_settings().litellm_base_url.rstrip("/")


def get_litellm_key() -> str:
    return get_settings().litellm_api_key
