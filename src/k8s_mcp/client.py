"""Cached ApiClient factory.

The Kubernetes python-client maintains a process-wide ``Configuration`` that
``ApiClient`` wraps. We cache a single ``ApiClient`` and rebuild it only when
auth-relevant settings change.

中文说明：
K8s python-client 的 ``Configuration`` 是进程级单例。本模块做一层轻量缓存：
当认证相关的 settings 没变时复用同一 ApiClient（避免每次 tool 调用都重建
HTTP 连接），只有认证字段变化时才重新构造。
"""
from __future__ import annotations

import logging

from kubernetes import client

from .auth import load_configuration
from .config import Settings, get_settings

logger = logging.getLogger(__name__)

_cached_client: client.ApiClient | None = None
_cached_key: tuple | None = None


# HTTP timeout / pool defaults — applied to every Configuration we hand to
# the python-client. The python-client's `Configuration` defaults are
# effectively infinite, which is exactly the wrong answer for a long-running
# MCP session where a half-dead apiserver would otherwise hang tool calls
# indefinitely.
_DEFAULT_CONN_TIMEOUT = 5   # seconds — TCP connect / TLS handshake
_DEFAULT_READ_TIMEOUT = 30  # seconds — per-response read


def _client_key(settings: Settings) -> tuple:
    """组成认证配置的 hashable key。"""
    return (
        settings.api_server,
        settings.api_token,
        settings.api_ca_cert,
        settings.api_insecure,
        settings.kubeconfig,
        settings.kube_context,
    )


def _apply_timeouts(configuration: client.Configuration) -> None:
    """Stamp HTTP timeouts onto the Configuration. Tolerates older kubernetes
    client versions where these fields don't exist — only set what we can."""
    try:
        configuration.conn_timeout = _DEFAULT_CONN_TIMEOUT
    except AttributeError:
        pass
    try:
        configuration.read_timeout = _DEFAULT_READ_TIMEOUT
    except AttributeError:
        pass


def get_api_client(settings: Settings | None = None) -> client.ApiClient:
    """返回根据当前 settings 缓存的 ApiClient。

    中文说明：
    所有 tool 函数都通过本方法拿 ApiClient；当认证相关的 settings 字段
    变化（切换 kubeconfig / token 等）时会自动重建。每次新建的
    Configuration 都会配上 conn_timeout / read_timeout，避免长跑
    MCP 会话里 apiserver 半死不活时 tool 调用挂死。
    """
    global _cached_client, _cached_key
    settings = settings or get_settings()
    key = _client_key(settings)
    if _cached_client is None or key != _cached_key:
        configuration = load_configuration(settings)
        _apply_timeouts(configuration)
        _cached_client = client.ApiClient(configuration)
        _cached_key = key
        logger.debug("Built new ApiClient (key=%s)", key)
    return _cached_client


def reset_client_cache() -> None:
    """清掉 ApiClient 缓存。测试场景切换时调用。"""
    global _cached_client, _cached_key
    _cached_client = None
    _cached_key = None
