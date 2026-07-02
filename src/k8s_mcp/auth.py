"""Kubernetes authentication: three auto-detected modes.

Priority:
  A. apiserver URL + token   (settings.api_server AND settings.api_token set)
  B. kubeconfig              (settings.kubeconfig path or default ~/.kube/config)
  C. in-cluster              (auto-detected via service account token file)

中文说明：
认证自动选择三档之一，按优先级匹配：

  - 模式 A：`K8S_MCP_API_SERVER` + `K8S_MCP_API_TOKEN` 都设置 → 直连 apiserver
  - 模式 B：`K8S_MCP_KUBECONFIG` 显式路径，或 `KUBECONFIG` 环境变量，
    或默认 `~/.kube/config`
  - 模式 C：检测到 `/var/run/secrets/kubernetes.io/serviceaccount/token`
    → 集群内 SA 模式（sidecar / pod 内运行时）

任意一种匹配成功即可，三档全失败则抛 AuthError。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from kubernetes import client
from kubernetes.config import (
    ConfigException,
    load_incluster_config,
    load_kube_config,
)

from .config import Settings

logger = logging.getLogger(__name__)

IN_CLUSTER_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"


class AuthError(Exception):
    """Raised when no auth mode is configured or the configured mode fails."""


def is_in_cluster() -> bool:
    """True when the standard in-cluster service-account token file is present."""
    return os.path.exists(IN_CLUSTER_TOKEN_PATH)


def _load_token_config(settings: Settings) -> client.Configuration:
    """Mode A: explicit apiserver URL + bearer token."""
    assert settings.api_server and settings.api_token
    cfg = client.Configuration()
    cfg.host = settings.api_server.rstrip("/")
    cfg.api_key_prefix = {"authorization": "Bearer"}
    cfg.api_key = {"authorization": settings.api_token}

    if settings.api_insecure:
        cfg.verify_ssl = False
    elif settings.api_ca_cert:
        ca_path = Path(settings.api_ca_cert).expanduser()
        if not ca_path.exists():
            raise AuthError(f"CA cert not found: {ca_path}")
        cfg.ssl_ca_cert = str(ca_path)
    # else: leave verify_ssl=True and use system CA bundle
    return cfg


def _load_kube_config(settings: Settings) -> client.Configuration:
    """Mode B: kubeconfig file (explicit path or KUBECONFIG env or default)."""
    import os

    kubeconfig_path = (
        str(Path(settings.kubeconfig).expanduser()) if settings.kubeconfig else None
    )
    # When no explicit path, fall back to KUBECONFIG env (which the kubernetes
    # client also reads), or the default ~/.kube/config location.
    if kubeconfig_path is None:
        env_kc = os.environ.get("KUBECONFIG")
        if env_kc:
            kubeconfig_path = env_kc.split(os.pathsep)[0]
        else:
            default = Path.home() / ".kube" / "config"
            if default.exists():
                kubeconfig_path = str(default)

    if not kubeconfig_path or not Path(kubeconfig_path).exists():
        raise AuthError(
            f"kubeconfig not found (path={kubeconfig_path!r}). "
            "Set K8S_MCP_KUBECONFIG or KUBECONFIG env var."
        )

    try:
        load_kube_config(
            config_file=kubeconfig_path,
            context=settings.kube_context,
        )
    except ConfigException as e:
        raise AuthError(f"Failed to load kubeconfig ({kubeconfig_path}): {e}") from e
    return client.Configuration.get_default_copy()


def _load_incluster() -> client.Configuration:
    """Mode C: in-cluster service-account."""
    try:
        load_incluster_config()
    except ConfigException as e:
        raise AuthError(f"Failed to load in-cluster config: {e}") from e
    return client.Configuration.get_default_copy()


def load_configuration(settings: Settings) -> client.Configuration:
    """Pick and load the right auth mode based on settings. See module docstring."""
    if settings.api_server and settings.api_token:
        logger.info("Auth mode A: apiserver URL + token (%s)", settings.api_server)
        return _load_token_config(settings)

    if settings.kubeconfig:
        logger.info("Auth mode B: kubeconfig path=%s", settings.kubeconfig)
        return _load_kube_config(settings)

    if is_in_cluster():
        logger.info("Auth mode C: in-cluster service account")
        return _load_incluster()

    # Last try: default kubeconfig location
    default_kc = Path.home() / ".kube" / "config"
    if default_kc.exists() or os.environ.get("KUBECONFIG"):
        logger.info("Auth mode B: default kubeconfig")
        return _load_kube_config(settings)

    raise AuthError(
        "No auth mode available. Set one of:\n"
        "  - K8S_MCP_API_SERVER + K8S_MCP_API_TOKEN (mode A)\n"
        "  - K8S_MCP_KUBECONFIG (mode B)\n"
        "  - Run inside a Kubernetes pod (mode C)\n"
        "  - Place kubeconfig at ~/.kube/config or set KUBECONFIG env var"
    )
