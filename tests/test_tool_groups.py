"""K8S_MCP_ENABLED_GROUPS tool-surface filtering.

Covers: default (all groups → full 91-tool inventory), single-group and
multi-group subsets, unknown-group rejection at config load, empty-string
treated as unset, and the `doctor` payload reflecting the setting.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from k8s_mcp.config import Settings, get_settings, reset_settings_cache
from k8s_mcp.server import _doctor_payload, create_server
from k8s_mcp.tool_groups import ALL_TOOL_GROUPS, TOOL_GROUP_MODULES

# The 13 read-only NVIDIA GPU tools registered by the `gpu` group.
_GPU_TOOLS = frozenset({
    "gpu_cluster_overview",
    "gpu_diagnose",
    "gpu_node_inspect",
    "gpu_pending_workloads",
    "gpu_workload_inspect",
    "gpu_metrics_catalog",
    "gpu_utilization_overview",
    "gpu_workload_utilization",
    "gpu_utilization_history",
    "gpu_mig_overview",
    "gpu_dra_overview",
    "gpu_capacity_analyze",
    "gpu_idle_resources",
})

# Full inventory (94 group tools + ping), mirrors tests/test_tool_inventory.py.
_FULL_INVENTORY_COUNT = 95


def _server_tools() -> frozenset[str]:
    reset_settings_cache()
    try:
        mcp = create_server()
        return frozenset(mcp._tool_manager._tools.keys())
    finally:
        reset_settings_cache()


def test_group_catalog_covers_all_modules():
    """Every group must be non-empty and group names unique by dict construction."""
    assert len(TOOL_GROUP_MODULES) == 6
    assert all(names for names in TOOL_GROUP_MODULES.values())
    assert ALL_TOOL_GROUPS == {"core", "workload", "observability", "security",
                               "gpu", "notify"}


def test_default_registers_full_inventory():
    tools = _server_tools()
    assert len(tools) == _FULL_INVENTORY_COUNT
    assert "ping" in tools
    assert _GPU_TOOLS <= tools


def test_gpu_group_only(monkeypatch):
    monkeypatch.setenv("K8S_MCP_ENABLED_GROUPS", "gpu")
    tools = _server_tools()
    assert tools == {"ping"} | _GPU_TOOLS


def test_core_group_excludes_write_and_gpu_families(monkeypatch):
    monkeypatch.setenv("K8S_MCP_ENABLED_GROUPS", "core")
    tools = _server_tools()
    # core present
    assert {"ping", "list_resources", "get_resource", "apply_yaml",
            "delete_resource", "get_pod_logs", "list_events"} <= tools
    # other groups absent
    assert not (_GPU_TOOLS & tools)
    assert "prometheus_query" not in tools
    assert "create_deployment" not in tools
    assert "analyze_rbac" not in tools
    assert "notify" not in tools


def test_multi_group_subset(monkeypatch):
    monkeypatch.setenv("K8S_MCP_ENABLED_GROUPS", "core,gpu")
    tools = _server_tools()
    assert _GPU_TOOLS <= tools
    assert "apply_yaml" in tools
    assert "prometheus_query" not in tools
    assert "create_deployment" not in tools
    assert "notify" not in tools


def test_whitespace_and_case_are_tolerated(monkeypatch):
    monkeypatch.setenv("K8S_MCP_ENABLED_GROUPS", " GPU , core ")
    tools = _server_tools()
    assert _GPU_TOOLS <= tools
    assert "delete_resource" in tools
    assert "notify" not in tools


def test_empty_string_means_all_groups(monkeypatch):
    monkeypatch.setenv("K8S_MCP_ENABLED_GROUPS", "")
    assert len(_server_tools()) == _FULL_INVENTORY_COUNT


def test_unknown_group_rejected_at_config_load(monkeypatch):
    monkeypatch.setenv("K8S_MCP_ENABLED_GROUPS", "core,nope")
    reset_settings_cache()
    try:
        with pytest.raises(ValidationError, match="unknown tool group"):
            get_settings()
    finally:
        reset_settings_cache()


def test_settings_accepts_list_input():
    s = Settings(enabled_groups=["gpu", "core"])
    assert s.enabled_groups == ["gpu", "core"]
    assert Settings(enabled_groups=None).enabled_groups is None


def test_doctor_payload_reports_enabled_groups():
    assert _doctor_payload(Settings())["enabled_groups"] == "all"
    assert _doctor_payload(Settings(enabled_groups=["gpu"]))["enabled_groups"] == ["gpu"]


def test_prometheus_unavailable_hint_adapts_to_groups(monkeypatch):
    """The failure hint must not promote find_prometheus_service() when the
    observability group (its group) is disabled."""
    from k8s_mcp.tools import nvidia_metrics

    # Default: all groups → hint promotes the discovery tool.
    assert "find_prometheus_service()" in nvidia_metrics._prometheus_unavailable("T", RuntimeError("down"))

    # observability disabled → hint explains the trim instead.
    monkeypatch.setenv("K8S_MCP_ENABLED_GROUPS", "gpu")
    reset_settings_cache()
    try:
        hint = nvidia_metrics._prometheus_unavailable("T", RuntimeError("down"))
        assert "find_prometheus_service()" not in hint.split("Set `K8S_MCP_PROMETHEUS_URL`")[0]
        assert "observability" in hint and "not registered" in hint
    finally:
        reset_settings_cache()
