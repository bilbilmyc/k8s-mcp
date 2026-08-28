"""Tool group definitions backing `K8S_MCP_ENABLED_GROUPS`.

The full inventory is 91 tools; a client that only operates GPU clusters
(or only reads) should not have to expose every tool to its LLM. Groups
let operators trim the tool surface per deployment without code changes:

    K8S_MCP_ENABLED_GROUPS=core,gpu        # only these groups
    K8S_MCP_ENABLED_GROUPS=                # unset/empty = all groups

中文说明：
分组是纯数据（组名 → tools/ 模块名），server.py、config.py 的校验器、
`doctor` 输出和测试都以这里为单一事实来源。`ping`（server.py 内置）
不属于任何组，永远注册。

Group semantics:

  - core          资源检索/读写删、日志事件、配置、发现 — 任何部署都需要
  - workload      工作负载/Service/存储/节点等写入与运维
  - observability 指标、Prometheus、健康与诊断分析
  - security      RBAC 与 NetworkPolicy 分析
  - gpu           NVIDIA GPU 诊断与 GPU 指标
  - notify        webhook 告警通知
"""
from __future__ import annotations

# Group name → tool module names (each module exposes `register(mcp)`).
TOOL_GROUP_MODULES: dict[str, tuple[str, ...]] = {
    "core": (
        "generic",
        "logs",
        "events",
        "pods",
        "configmap",
        "namespace",
        "secret",
        "delete_tool",
        "jsonpath",
        "discovery",
        "wait_tool",
        "cluster_info",
    ),
    "workload": (
        "workload",
        "service",
        "storage",
        "autoscale",
        "rollout",
        "node_ops",
        "certs",
        "serviceaccount",
    ),
    "observability": (
        "metrics",
        "prometheus",
        "health",
        "diagnostics",
        "explain",
        "resource_usage",
    ),
    "security": (
        "rbac",
        "networkpolicy",
    ),
    "gpu": (
        "nvidia_gpu",
        "nvidia_metrics",
        "gpu_capacity",
    ),
    "notify": (
        "notifier",
    ),
}

ALL_TOOL_GROUPS: frozenset[str] = frozenset(TOOL_GROUP_MODULES)


def unknown_groups(enabled: list[str]) -> list[str]:
    """Return group names in `enabled` that are not defined here, sorted."""
    return sorted(set(enabled) - ALL_TOOL_GROUPS)
