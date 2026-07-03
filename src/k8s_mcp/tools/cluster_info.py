"""Cluster identity: apiserver URL, K8s version, platform, counts.

`cluster_info` is the "what am I talking to" tool. The first call in any
new MCP session should usually be this (or `whoami`) — knowing the
cluster version and current context shapes which features the agent
can rely on (e.g. PodDisruptionBudget v1 requires 1.21+, IngressClass
needs 1.18+, Gateway API is opt-in, etc.).

中文说明：
集群基本信息查询。返回 apiserver URL、K8s 版本、平台（linux/amd64）、
节点数、namespace 数、Pod 数。是新会话里通常第一个会调的工具——
让 Agent 知道它在跟什么版本的集群对话，影响后续的兼容性判断。
"""
from __future__ import annotations

import logging

from kubernetes import client
from kubernetes.client.rest import ApiException

from ..client import get_api_client

logger = logging.getLogger(__name__)


def cluster_info() -> str:
    """ℹ️  CLUSTER INFO — apiserver URL, version, platform, and live counts.

    Returns a multi-section report:
        ## Connection
          - apiserver URL (from the configured ApiClient)
          - bearer token in use (yes/no — never the value)
        ## Cluster
          - GitVersion, GitCommit, Major/Minor
          - Platform (e.g. linux/amd64)
        ## Counts (live)
          - nodes
          - namespaces
          - pods (cluster-wide, may be slow on huge clusters)
          - services, deployments (apps/v1)

    All counts are best-effort — a single failing section does not blank
    the rest.
    """
    api_client = get_api_client()
    cfg = api_client.configuration
    server = cfg.host or "(unknown)"
    api_key = getattr(cfg, "api_key", None) or {}
    has_token = bool(api_key.get("Bearer")) or bool(api_key)

    lines = ["## Connection"]
    lines.append(f"Server:        {server}")
    lines.append(f"Bearer token:  {'yes' if has_token else 'no (in-cluster SA or no auth)'}")

    # Version
    try:
        v = client.VersionApi(api_client).get_code()
        lines.append("")
        lines.append("## Cluster")
        lines.append(f"GitVersion:    {v.git_version}")
        if v.git_commit:
            lines.append(f"GitCommit:     {v.git_commit[:12]}")
        if v.major and v.minor:
            lines.append(f"Version:       {v.major}.{v.minor}")
        if v.platform:
            lines.append(f"Platform:      {v.platform}")
    except ApiException as e:
        lines.append("")
        lines.append("## Cluster")
        lines.append(f"(version fetch failed: {e.status} {e.reason})")

    core = client.CoreV1Api(api_client)
    apps = client.AppsV1Api(api_client)

    # Counts
    lines.append("")
    lines.append("## Counts")
    sections: list[tuple[str, int | str]] = []
    for label, fetcher in (
        ("Nodes",       lambda: len(core.list_node().items)),
        ("Namespaces",  lambda: len(core.list_namespace().items)),
        ("Pods",        lambda: len(core.list_pod_for_all_namespaces().items)),
        ("Services",    lambda: len(core.list_service_for_all_namespaces().items)),
        ("Deployments", lambda: len(apps.list_deployment_for_all_namespaces().items)),
    ):
        try:
            sections.append((label, fetcher()))
        except ApiException as e:
            sections.append((label, f"error: {e.status} {e.reason}"))
    for label, value in sections:
        lines.append(f"{label + ':':14}{value}")

    return "\n".join(lines)


def register(mcp) -> None:
    mcp.tool()(cluster_info)
