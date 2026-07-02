"""Cached ApiClient factory.

The Kubernetes python-client maintains a process-wide ``Configuration`` that
``ApiClient`` wraps. We cache a single ``ApiClient`` and rebuild it only when
auth-relevant settings change.
"""
from __future__ import annotations

import logging

from kubernetes import client

from .auth import load_configuration
from .config import Settings, get_settings

logger = logging.getLogger(__name__)

_cached_client: client.ApiClient | None = None
_cached_key: tuple | None = None


def _client_key(settings: Settings) -> tuple:
    """Hashable key that uniquely identifies an auth configuration."""
    return (
        settings.api_server,
        settings.api_token,
        settings.api_ca_cert,
        settings.api_insecure,
        settings.kubeconfig,
        settings.kube_context,
    )


def get_api_client(settings: Settings | None = None) -> client.ApiClient:
    """Return a cached ApiClient built from current settings."""
    global _cached_client, _cached_key
    settings = settings or get_settings()
    key = _client_key(settings)
    if _cached_client is None or key != _cached_key:
        configuration = load_configuration(settings)
        _cached_client = client.ApiClient(configuration)
        _cached_key = key
        logger.debug("Built new ApiClient (key=%s)", key)
    return _cached_client


def reset_client_cache() -> None:
    """Drop the cached client. Tests should call this between scenarios."""
    global _cached_client, _cached_key
    _cached_client = None
    _cached_key = None
