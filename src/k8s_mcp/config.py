"""Runtime settings for k8s-mcp.

All environment variables are prefixed with K8S_MCP_.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="K8S_MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Logging / output
    log_level: str = "INFO"
    default_tail_lines: int = 100

    # Auth mode A: apiserver URL + token
    api_server: str | None = None
    api_token: str | None = None
    api_ca_cert: str | None = None
    api_insecure: bool = False

    # Auth mode B: kubeconfig
    kubeconfig: str | None = None
    kube_context: str | None = None

    # Safety
    read_only: bool = False
    namespace_allowlist: list[str] | None = None
    delete_token_secret: str = "change-me"
    delete_token_ttl_seconds: int = 300

    @field_validator("namespace_allowlist", mode="before")
    @classmethod
    def _split_allowlist(cls, v: Any) -> list[str] | None:
        """Parse comma-separated env var values, and treat empty string as None."""
        if v is None or v == "":
            return None
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    def ns_allowed(self, namespace: str | None) -> bool:
        """Return True if writes are allowed for the given namespace."""
        if self.read_only:
            return False
        if self.namespace_allowlist is None:
            return True
        if namespace is None:
            return False  # cluster-scoped writes not allowed when allowlist set
        return namespace in self.namespace_allowlist


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Clear the cached settings (useful for tests)."""
    get_settings.cache_clear()
