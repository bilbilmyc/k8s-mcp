"""Cached Kubernetes client factories.

The Kubernetes python-client maintains a process-wide ``Configuration`` that
``ApiClient`` wraps. We cache one ``ApiClient`` and one ``DynamicClient`` and
rebuild them only when transport- or auth-relevant settings change.

中文说明：
K8s python-client 的 ``Configuration`` 是进程级单例。本模块做一层轻量缓存：
当 settings 没变时复用 ApiClient 与 DynamicClient（避免每次 tool 调用都重建
HTTP 连接、读取 discovery cache），只有认证或并发配置变化时才重新构造。
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from typing import TYPE_CHECKING

from kubernetes import client

from .auth import load_configuration
from .config import Settings, get_settings

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

_cached_client: client.ApiClient | None = None
_cached_key: tuple | None = None
_cached_dynamic_client: DynamicClient | None = None
_cached_dynamic_key: tuple | None = None
_api_cache_lock = threading.RLock()
_dynamic_cache_lock = threading.RLock()
_dynamic_discovery_lock = threading.RLock()


# HTTP timeout / pool defaults. Kubernetes has no effective request timeout
# unless `_request_timeout` reaches its transport, so `_BoundedApiClient`
# supplies one when generated and dynamic calls omit it.
_DEFAULT_CONN_TIMEOUT = 5   # seconds — TCP connect / TLS handshake
_DEFAULT_READ_TIMEOUT = 30  # seconds — per-response read
_MIN_CONNECTION_POOL_SIZE = 8


class _BoundedApiClient(client.ApiClient):
    """ApiClient that supplies a real urllib3 timeout when callers omit one."""

    def request(
        self,
        method,
        url,
        query_params=None,
        headers=None,
        post_params=None,
        body=None,
        _preload_content=True,
        _request_timeout=None,
    ):
        if _request_timeout is None:
            _request_timeout = (_DEFAULT_CONN_TIMEOUT, _DEFAULT_READ_TIMEOUT)
        return super().request(
            method,
            url,
            query_params=query_params,
            headers=headers,
            post_params=post_params,
            body=body,
            _preload_content=_preload_content,
            _request_timeout=_request_timeout,
        )


class _LockedDiscoverer:
    """Serialize lazy discovery-cache mutations while leaving API calls parallel."""

    def __init__(self, discoverer) -> None:
        self._discoverer = discoverer

    def get(self, **kwargs):
        with _dynamic_discovery_lock:
            return self._discoverer.get(**kwargs)

    def search(self, **kwargs):
        with _dynamic_discovery_lock:
            return self._discoverer.search(**kwargs)

    def __iter__(self):
        def _iterate():
            with _dynamic_discovery_lock:
                yield from self._discoverer

        return _iterate()

    def __getattr__(self, name):
        with _dynamic_discovery_lock:
            return getattr(self._discoverer, name)


class _DynamicClientProxy:
    """Expose a DynamicClient with a synchronized lazy discoverer."""

    def __init__(self, dynamic_client) -> None:
        self._dynamic_client = dynamic_client
        self.resources = _LockedDiscoverer(dynamic_client.resources)

    def __getattr__(self, name):
        return getattr(self._dynamic_client, name)


def _secret_fingerprint(value: str | None) -> str | None:
    """Return a stable cache token without retaining the credential itself."""
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def client_cache_key(settings: Settings | None = None) -> tuple:
    """Return a non-secret identity for Kubernetes auth and transport state."""
    settings = settings or get_settings()
    return (
        settings.api_server,
        _secret_fingerprint(settings.api_token),
        settings.api_ca_cert,
        settings.api_insecure,
        settings.kubeconfig,
        settings.kube_context,
        os.environ.get("KUBECONFIG"),
        settings.max_concurrent_tools,
    )


def _close_api_client(api_client: client.ApiClient | None) -> None:
    """Release urllib3 and async worker pools owned by an obsolete client."""
    if api_client is None:
        return
    try:
        api_client.rest_client.pool_manager.clear()
    except Exception as exc:  # noqa: BLE001 - cleanup must not hide the replacement client
        logger.debug("Could not clear obsolete Kubernetes connection pool: %s", exc)
    try:
        api_client.close()
    except Exception as exc:  # noqa: BLE001 - cleanup must not hide the replacement client
        logger.debug("Could not close obsolete Kubernetes ApiClient: %s", exc)


def _connection_pool_size(settings: Settings) -> int:
    """Keep enough reusable connections for tool and inner fan-out workers."""
    return max(_MIN_CONNECTION_POOL_SIZE, settings.max_concurrent_tools * 2)


def transport_defaults(settings: Settings) -> dict[str, int]:
    """Return the effective non-secret transport defaults for diagnostics."""
    return {
        "connect_timeout_s": _DEFAULT_CONN_TIMEOUT,
        "read_timeout_s": _DEFAULT_READ_TIMEOUT,
        "connection_pool_size": _connection_pool_size(settings),
    }


def _apply_transport_defaults(
    configuration: client.Configuration,
    settings: Settings,
) -> None:
    """Apply an automatically sized reusable connection pool."""
    try:
        configuration.connection_pool_maxsize = _connection_pool_size(settings)
    except AttributeError:
        pass


def get_api_client(settings: Settings | None = None) -> client.ApiClient:
    """返回根据当前 settings 缓存的 ApiClient。

    中文说明：
    所有 tool 函数都通过本方法拿 ApiClient；当认证相关的 settings 字段
    变化（切换 kubeconfig / token 等）时会自动重建。每次新建的
    client 都会配上默认 connect / read timeout，避免长跑 MCP 会话里
    apiserver 半死不活时 tool 调用挂死。
    """
    global _cached_client, _cached_key
    settings = settings or get_settings()
    key = client_cache_key(settings)
    previous_client: client.ApiClient | None = None
    with _api_cache_lock:
        if _cached_client is None or key != _cached_key:
            configuration = load_configuration(settings)
            _apply_transport_defaults(configuration, settings)
            replacement = _BoundedApiClient(configuration)
            previous_client = _cached_client
            _cached_client = replacement
            _cached_key = key
            logger.debug(
                "Built Kubernetes ApiClient (pool_size=%d)",
                configuration.connection_pool_maxsize,
            )
        result = _cached_client
    _close_api_client(previous_client)
    return result


def get_dynamic_client(settings: Settings | None = None) -> DynamicClient:
    """Return the process-wide DynamicClient for the effective configuration.

    DynamicClient construction reads and decodes the Kubernetes discovery
    cache. Reusing it avoids repeating that work in every generic, RBAC,
    JSONPath, Secret, and wait-tool call.
    """
    global _cached_dynamic_client, _cached_dynamic_key
    settings = settings or get_settings()
    api_client = get_api_client(settings)
    # Include object identity so a rebuilt ApiClient is never paired with a
    # DynamicClient that still points at the closed predecessor.
    key = (client_cache_key(settings), id(api_client))
    with _dynamic_cache_lock:
        if _cached_dynamic_client is None or key != _cached_dynamic_key:
            from kubernetes import dynamic

            raw_client = dynamic.DynamicClient(api_client)
            _cached_dynamic_client = _DynamicClientProxy(raw_client)
            _cached_dynamic_key = key
            logger.debug("Built Kubernetes DynamicClient")
        return _cached_dynamic_client


def reset_client_cache() -> None:
    """清掉 ApiClient 与 DynamicClient 缓存。测试场景切换时调用。"""
    global _cached_client, _cached_key, _cached_dynamic_client, _cached_dynamic_key
    with _dynamic_cache_lock:
        _cached_dynamic_client = None
        _cached_dynamic_key = None
    with _api_cache_lock:
        previous_client = _cached_client
        _cached_client = None
        _cached_key = None
    _close_api_client(previous_client)
