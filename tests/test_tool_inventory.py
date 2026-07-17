"""Pin the tool inventory at 87 entries.

This is a guard against accidental additions/removals that change the
tool surface LLM agents see. If you intentionally add or remove a tool,
update BOTH the expected count AND the expected set in one shot.

`EXPECTED_TOOLS` is the canonical list of tools registered on the
FastMCP instance after v0.7.0 (NVIDIA GPU diagnostics added — see
`docs/CHANGELOG.md` [Unreleased] for details).
Adding a new tool requires updating this list in the same PR.
"""
from __future__ import annotations

import pytest

EXPECTED_TOOL_COUNT = 90
EXPECTED_TOOLS: frozenset[str] = frozenset({
    "ping",
    # autoscale
    "create_hpa",
    "create_pdb",
    # certs
    "get_certificate_expiry",
    # cluster_info
    "cluster_info",
    # configmap
    "create_configmap",
    "get_configmap",
    "update_configmap",
    # delete_tool
    "delete_resource",
    # diagnostics
    "diagnose_deployment",
    "diagnose_pod",
    # discovery
    "explain_resource",
    "find_images",
    "get_api_resources",
    # events
    "get_events_for_object",
    "list_events",
    # explain
    "explain_pod",
    # generic
    "add_label",
    "apply_yaml",
    "describe_resource",
    "diff_resource",
    "get_resource",
    "get_resource_yaml",
    "list_resources",
    "remove_label",
    "replace_resource",
    "search_resources",
    # health
    "cluster_health_snapshot",
    # jsonpath
    "get_resource_jsonpath",
    # logs
    "get_pod_logs",
    # metrics
    "bootstrap_metrics_server",
    "top_nodes",
    "top_pods",
    # namespace (new in v0.6.0)
    "create_namespace",
    # networkpolicy
    "analyze_networkpolicy",
    "create_networkpolicy",
    # node_ops (extended in v0.6.0)
    "cordon_node",
    "drain_node",
    "label_node",
    "list_nodes",
    "taint_node",
    "unlabel_node",
    "uncordon_node",
    "untaint_node",
    # NVIDIA GPU diagnostics (read-only)
    "gpu_cluster_overview",
    "gpu_diagnose",
    "gpu_node_inspect",
    "gpu_pending_workloads",
    "gpu_workload_inspect",
    "gpu_metrics_catalog",
    "gpu_utilization_overview",
    "gpu_workload_utilization",
    # notifier
    "notify",
    # pods
    "exec_pod",
    "list_pods",
    # prometheus
    "expose_prometheus_as_nodeport",
    "find_prometheus_service",
    "pod_metrics",
    "prometheus_query",
    "prometheus_query_range",
    # rbac
    "analyze_rbac",
    "create_clusterrole",
    "create_clusterrolebinding",
    "create_role",
    "create_rolebinding",
    "whoami",
    # resource_usage
    "analyze_resource_usage",
    # rollout
    "rollout_history",
    "rollout_status",
    "rollout_undo",
    # secret (extended in v0.6.0)
    "create_secret",
    "get_secret_value",
    "list_secrets",
    # service (extended in v0.6.0)
    "create_ingress",
    "create_service",
    "expose_workload",
    "get_endpoints",
    # serviceaccount
    "create_serviceaccount",
    # storage
    "bootstrap_local_path_provisioner",
    "create_pvc",
    "validate_pv_hostpath_paths",
    # wait_tool
    "wait_resource",
    # workload
    "create_cronjob",
    "create_deployment",
    "create_job",
    "create_statefulset",
    "restart_workload",
    "scale_workload",
    "set_image",
    "set_resources",
})


@pytest.fixture
def server_tools():
    """Build the server and return the set of registered tool names.

    Function-scoped (not module) because the autouse `_clean_env`
    conftest fixture wipes K8S_MCP_* env vars per-test; a module-scoped
    fixture would inherit the first test's env.
    """
    from k8s_mcp.config import reset_settings_cache
    from k8s_mcp.server import create_server

    reset_settings_cache()
    mcp = create_server()
    yield frozenset(mcp._tool_manager._tools.keys())
    reset_settings_cache()


def test_tool_count_matches_expected(server_tools):
    """Pin the total number of tools. Changing this requires updating
    `EXPECTED_TOOL_COUNT` and `EXPECTED_TOOLS` together — the guard at
    the bottom will refuse to accept a one-sided change."""
    assert len(server_tools) == EXPECTED_TOOL_COUNT, (
        f"expected {EXPECTED_TOOL_COUNT} tools, got {len(server_tools)}: "
        f"added={server_tools - EXPECTED_TOOLS}, "
        f"removed={EXPECTED_TOOLS - server_tools}"
    )


def test_tool_set_matches_expected(server_tools):
    """Pin the *identity* of every tool. Catches subtle changes (rename,
    accidental merge of two tools, removal of a deprecated alias) even
    if the count stays the same."""
    assert server_tools == EXPECTED_TOOLS, (
        f"tool set drifted: added={server_tools - EXPECTED_TOOLS}, "
        f"removed={EXPECTED_TOOLS - server_tools}"
    )


def test_no_deprecated_tools_registered(server_tools):
    """Defense-in-depth: the 9 deprecated tools removed in v0.5.0 must
    never reappear in the registry. Catches accidental re-introduction
    via a merge conflict or a `git revert`."""
    forbidden = {
        "bulk_set_image", "bulk_restart", "bulk_scale", "bulk_delete_pvc",
        "delete_pod", "delete_pvc", "delete_service", "delete_ingress",
        "delete_configmap",
    }
    leaked = forbidden & server_tools
    assert not leaked, (
        f"deprecated tools must not be registered: {leaked}. "
        "If this was intentional, also remove this guard."
    )
