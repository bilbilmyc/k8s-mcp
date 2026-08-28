"""Tests for the metrics cascade (top_pods / top_nodes + bootstrap_metrics_server).

The non-trivial behavior is the **cascade contract**: top_pods / top_nodes
must transparently fall back from metrics-server to Prometheus without
installing components as a read side effect. The error message when both
paths fail must literally name every
next-step tool the agent can call, otherwise the agent fixates on
"install metrics-server" and never reaches for `find_prometheus_service()`.

Tests split by cascade path:
  - `test_top_*_via_metrics_server_returns_table` — path 1 happy.
  - `test_top_*_via_prometheus_when_metrics_missing` — path 1 fails,
    path 2 succeeds. Mocked at the prometheus._query_instant boundary.
  - `test_top_*_error_when_both_missing_and_read_only` — paths 1 + 2
    fail, path 3 unavailable (READ_ONLY). Error names bootstrap_metrics_server,
    find_prometheus_service(), prometheus_query().
  - `test_top_*_never_auto_bootstraps` — paths 1 + 2 fail and the explicit
    bootstrap tool is not invoked.

bootstrap_metrics_server itself:
  - `test_bootstrap_metrics_server_idempotent` — Deployment already exists
    short-circuits with status=AlreadyInstalled.
  - `test_bootstrap_metrics_server_read_only_rejected`.
  - `test_bootstrap_metrics_server_allowlist_excludes_kube_system_rejected`.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException

from k8s_mcp.config import reset_settings_cache
from k8s_mcp.tools import metrics


@pytest.fixture(autouse=True)
def _clear(monkeypatch):
    monkeypatch.setattr(metrics, "get_api_client", lambda: object())
    reset_settings_cache()
    yield
    reset_settings_cache()


# ---------- helpers ----------------------------------------------------------


class _FakeCustomObjectsApi:
    """Simulates metrics-server returning 404 from the aggregated API."""

    def list_cluster_custom_object(self, *args, **kwargs):
        raise ApiException(status=404, reason="Not Found")

    def list_namespaced_custom_object(self, *args, **kwargs):
        raise ApiException(status=404, reason="Not Found")


def _mock_prometheus_query_instant(*row_lists: list[dict]):
    """Patch prometheus._query_instant to return one list per call.

    top_pods issues two queries (CPU, memory); top_nodes issues two
    (CPU, memory). Pass two row lists per call so the CPU query and
    memory query each return their own rows. The mock consumes lists
    in order via side_effect; if only one is given, it's reused for
    every call.
    """
    if len(row_lists) == 1:
        return patch.object(
            metrics.prom_mod, "_query_instant", return_value=row_lists[0]
        )
    return patch.object(
        metrics.prom_mod, "_query_instant", side_effect=list(row_lists)
    )


def _fake_pod_instant(metric: str, value: str, ns: str = "default", pod: str = "web-1"):
    """Build a Prometheus instant-query result row."""
    return {
        "metric": {"namespace": ns, "pod": pod, "__name__": metric},
        "value": [1234567890.0, value],
    }


def _fake_node_instant(metric: str, value: str, node: str = "deploy"):
    return {
        "metric": {"node": node, "__name__": metric},
        "value": [1234567890.0, value],
    }


# ---------- Path 1: metrics-server happy -------------------------------------


def test_top_pods_via_metrics_server_returns_table():
    """When metrics-server is installed, top_pods uses it directly and
    never falls through to Prometheus."""
    items = {
        "items": [
            {
                "metadata": {"name": "web-1", "namespace": "default"},
                "containers": [
                    {"usage": {"cpu": "100m", "memory": "256Mi"}},
                    {"usage": {"cpu": "50m", "memory": "64Mi"}},
                ],
            },
            {
                "metadata": {"name": "db-1", "namespace": "default"},
                "containers": [
                    {"usage": {"cpu": "500m", "memory": "1Gi"}},
                ],
            },
        ]
    }

    class _Ok:
        def list_cluster_custom_object(self, *a, **kw):
            return items

        def list_namespaced_custom_object(self, *a, **kw):
            return items

    with patch.object(metrics, "_custom_objects_api", lambda: _Ok()):
        out = metrics.top_pods(namespace="default")
    assert "web-1" in out
    assert "db-1" in out
    assert "150m" in out  # 100m + 50m


def test_top_nodes_via_metrics_server_returns_table():
    items = {
        "items": [
            {"metadata": {"name": "deploy"}, "usage": {"cpu": "500m", "memory": "2Gi"}},
        ]
    }

    class _Ok:
        def list_cluster_custom_object(self, *a, **kw):
            return items

        def list_namespaced_custom_object(self, *a, **kw):
            return items

    with patch.object(metrics, "_custom_objects_api", lambda: _Ok()):
        out = metrics.top_nodes()
    assert "deploy" in out
    assert "500m" in out
    assert "2.0Gi" in out


# ---------- Path 2: Prometheus fallback --------------------------------------


def test_top_pods_via_prometheus_when_metrics_missing():
    """metrics-server 404 → top_pods tries Prometheus → returns prom data."""
    fake_api = MagicMock()
    fake_api.return_value = _FakeCustomObjectsApi()
    cpu_rows = [
        _fake_pod_instant("container_cpu_usage_seconds_total", "0.15"),
    ]
    mem_rows = [
        _fake_pod_instant("container_memory_working_set_bytes", "268435456"),
    ]
    with patch.object(metrics, "_custom_objects_api", fake_api), \
         _mock_prometheus_query_instant(cpu_rows, mem_rows):
        out = metrics.top_pods(namespace="default")
    assert "web-1" in out
    # CPU 0.15 cores → 150m; memory 268435456 bytes → 256Mi.
    assert "150m" in out
    assert "256Mi" in out
    # The result header makes the data source visible to the agent.
    assert "Prometheus" in out


def test_top_pods_label_selector_translated_to_pod_regex():
    """label_selector on the Prometheus path is translated to a pod-name
    regex by listing pods with the selector first."""
    fake_metrics_api = MagicMock()
    fake_metrics_api.return_value = _FakeCustomObjectsApi()
    fake_core = MagicMock()
    pod1 = MagicMock()
    pod1.metadata.name = "nginx-aaa"
    pod2 = MagicMock()
    pod2.metadata.name = "nginx-bbb"
    fake_core.list_namespaced_pod.return_value.items = [pod1, pod2]

    cpu_rows = [
        _fake_pod_instant("container_cpu_usage_seconds_total", "0.02", pod="nginx-aaa"),
        _fake_pod_instant("container_cpu_usage_seconds_total", "0.03", pod="nginx-bbb"),
    ]
    mem_rows = [
        _fake_pod_instant("container_memory_working_set_bytes", "100000000", pod="nginx-aaa"),
        _fake_pod_instant("container_memory_working_set_bytes", "200000000", pod="nginx-bbb"),
    ]
    with patch.object(metrics, "_custom_objects_api", fake_metrics_api), \
         patch.object(metrics.client, "CoreV1Api", return_value=fake_core), \
         _mock_prometheus_query_instant(cpu_rows, mem_rows):
        out = metrics.top_pods(
            namespace="default", label_selector="app=nginx"
        )
    # The label-selector lookup happened.
    fake_core.list_namespaced_pod.assert_called_once_with(
        namespace="default", label_selector="app=nginx"
    )
    assert "nginx-aaa" in out
    assert "nginx-bbb" in out


def test_pod_selector_lookup_uses_shared_api_client(monkeypatch):
    api_client = object()
    captured = {}
    core = MagicMock()
    core.list_namespaced_pod.return_value.items = []
    monkeypatch.setattr(metrics, "get_api_client", lambda: api_client)

    def build_core(received):
        captured["api_client"] = received
        return core

    monkeypatch.setattr(metrics.client, "CoreV1Api", build_core)
    metrics._pods_matching_selector("default", "app=web")
    assert captured["api_client"] is api_client


def test_top_nodes_via_prometheus_when_metrics_missing():
    fake_api = MagicMock()
    fake_api.return_value = _FakeCustomObjectsApi()
    cpu_rows = [
        _fake_node_instant("node_cpu_seconds_total", "0.42"),
    ]
    mem_rows = [
        _fake_node_instant("node_memory_MemTotal_bytes", "8589934592"),
        _fake_node_instant("node_memory_MemAvailable_bytes", "4294967296"),
    ]
    with patch.object(metrics, "_custom_objects_api", fake_api), \
         _mock_prometheus_query_instant(cpu_rows, mem_rows):
        out = metrics.top_nodes()
    assert "deploy" in out
    assert "420m" in out  # 0.42 cores
    # Used = Total - Available = 4 GiB.
    assert "4.0Gi" in out


# ---------- Path 3: both fail, READ_ONLY → hint -----------------------------


def test_top_pods_error_when_both_missing_and_read_only(monkeypatch):
    """When metrics-server AND Prometheus fail AND we can't bootstrap
    (READ_ONLY), the error message must literally name the bootstrap
    tool and the Prometheus discovery path.

    Simulated Prometheus failure: `_query_instant` raises LookupError
    (no Prometheus URL resolvable). Empty result is treated as
    "reachable, no data" and does NOT escalate to bootstrap.
    """
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "true")
    reset_settings_cache()
    fake_api = MagicMock()
    fake_api.return_value = _FakeCustomObjectsApi()
    with patch.object(metrics, "_custom_objects_api", fake_api), \
         patch.object(
             metrics.prom_mod, "_query_instant",
             side_effect=LookupError("Prometheus is not auto-discoverable"),
         ):
        with pytest.raises(RuntimeError) as ei:
            metrics.top_pods(namespace="default")
    msg = str(ei.value)
    assert "bootstrap_metrics_server" in msg
    assert "find_prometheus_service()" in msg
    assert "prometheus_query(" in msg


def test_top_nodes_error_when_both_missing_and_read_only(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "true")
    reset_settings_cache()
    fake_api = MagicMock()
    fake_api.return_value = _FakeCustomObjectsApi()
    with patch.object(metrics, "_custom_objects_api", fake_api), \
         patch.object(
             metrics.prom_mod, "_query_instant",
             side_effect=LookupError("Prometheus is not auto-discoverable"),
         ):
        with pytest.raises(RuntimeError) as ei:
            metrics.top_nodes()
    msg = str(ei.value)
    assert "bootstrap_metrics_server" in msg
    assert "find_prometheus_service()" in msg


def test_top_pods_error_when_both_missing_and_allowlist_excludes_kube_system(monkeypatch):
    """When write perms exclude kube-system, bootstrap is refused with
    the same hint as READ_ONLY."""
    monkeypatch.setenv("K8S_MCP_NAMESPACE_ALLOWLIST", "default,app")
    reset_settings_cache()
    fake_api = MagicMock()
    fake_api.return_value = _FakeCustomObjectsApi()
    with patch.object(metrics, "_custom_objects_api", fake_api), \
         patch.object(
             metrics.prom_mod, "_query_instant",
             side_effect=LookupError("Prometheus is not auto-discoverable"),
         ):
        with pytest.raises(RuntimeError) as ei:
            metrics.top_pods()
    msg = str(ei.value)
    assert "bootstrap_metrics_server" in msg
    assert "kube-system" in msg  # the deny reason surfaces


# ---------- Path 3: bootstrap is always explicit ----------------------------


def test_top_pods_never_auto_bootstraps(monkeypatch):
    fake_metrics_api = MagicMock()
    fake_metrics_api.return_value = _FakeCustomObjectsApi()

    with patch.object(metrics, "_custom_objects_api", fake_metrics_api), \
         patch.object(
             metrics.prom_mod, "_query_instant",
             side_effect=LookupError("Prometheus is not auto-discoverable"),
         ), \
         patch.object(metrics, "bootstrap_metrics_server") as bootstrap:
        with pytest.raises(RuntimeError, match="NOT RUN automatically"):
            metrics.top_pods(namespace="default")
        bootstrap.assert_not_called()


def test_repeated_top_pods_never_auto_bootstraps(monkeypatch):
    fake_api = MagicMock()
    fake_api.return_value = _FakeCustomObjectsApi()
    with patch.object(metrics, "_custom_objects_api", fake_api), \
         patch.object(
             metrics.prom_mod, "_query_instant",
             side_effect=LookupError("Prometheus is not auto-discoverable"),
         ):
        with patch.object(metrics, "bootstrap_metrics_server") as bootstrap:
            with pytest.raises(RuntimeError):
                metrics.top_pods()
            with pytest.raises(RuntimeError):
                metrics.top_pods()
            bootstrap.assert_not_called()


# ---------- bootstrap_metrics_server tool itself ---------------------------


def _deployment_not_found_then_ready():
    """Side-effect sequence for AppsV1Api.read_namespaced_deployment:
    first 404 (probe), then a fake ready Deployment."""
    ready = MagicMock()
    ready.status.available_replicas = 1
    ready.spec.replicas = 1
    return [
        ApiException(status=404, reason="Not Found"),
        ready,
    ]


def test_bootstrap_metrics_server_installs_from_default_manifest(monkeypatch):
    """Happy path: apply the official manifest, patch the kubelet flag,
    wait for ready. We mock urllib + apply_yaml + apps API."""
    monkeypatch.delenv("K8S_MCP_METRICS_SERVER_MANIFEST_URL", raising=False)
    reset_settings_cache()

    fake_apps_api = MagicMock()
    # First read: 404 (probe — Deployment not installed yet).
    # Second read: would be the post-apply wait, but the wait helper is
    # patched to short-circuit, so this just exercises the idempotency
    # probe path.
    fake_apps_api.read_namespaced_deployment.side_effect = ApiException(
        status=404, reason="Not Found"
    )

    with patch.object(metrics, "apply_yaml") as mock_apply, \
         patch.object(metrics.client, "AppsV1Api", return_value=fake_apps_api), \
         patch.object(metrics, "_patch_metrics_server_kubelet_flag") as mock_patch, \
         patch.object(metrics, "_wait_for_deployment_ready", return_value=(1, 1, 5)), \
         patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"fake manifest"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        out = metrics.bootstrap_metrics_server()

    mock_apply.assert_called_once_with(b"fake manifest".decode("utf-8"))
    mock_patch.assert_called_once()
    assert "status=Installed" in out
    assert "waited 5s" in out
    # Probe read happened once. The post-apply wait is mocked.
    assert fake_apps_api.read_namespaced_deployment.call_count == 1


def test_bootstrap_metrics_server_idempotent_when_deployment_exists(monkeypatch):
    """If Deployment/metrics-server is already in kube-system, return
    status=AlreadyInstalled without re-applying."""
    existing = MagicMock()
    existing.status.available_replicas = 1
    existing.spec.replicas = 1
    fake_apps_api = MagicMock()
    fake_apps_api.read_namespaced_deployment.return_value = existing

    with patch.object(metrics, "apply_yaml") as mock_apply, \
         patch.object(metrics.client, "AppsV1Api", return_value=fake_apps_api):
        out = metrics.bootstrap_metrics_server()

    assert "status=AlreadyInstalled" in out
    mock_apply.assert_not_called()


def test_bootstrap_metrics_server_read_only_rejected(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "true")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="read-only"):
        metrics.bootstrap_metrics_server()


def test_bootstrap_metrics_server_allowlist_excludes_kube_system_rejected(monkeypatch):
    monkeypatch.setenv("K8S_MCP_NAMESPACE_ALLOWLIST", "default,app")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="kube-system"):
        metrics.bootstrap_metrics_server()


def test_bootstrap_metrics_server_custom_manifest_url(monkeypatch):
    """When `K8S_MCP_METRICS_SERVER_MANIFEST_URL` is set, fetch from there
    instead of the default upstream URL."""
    monkeypatch.setenv(
        "K8S_MCP_METRICS_SERVER_MANIFEST_URL", "https://internal.example/m.yaml"
    )
    reset_settings_cache()

    fake_apps_api = MagicMock()
    fake_apps_api.read_namespaced_deployment.side_effect = (
        _deployment_not_found_then_ready()
    )

    with patch.object(metrics, "apply_yaml"), \
         patch.object(metrics.client, "AppsV1Api", return_value=fake_apps_api), \
         patch.object(metrics, "_patch_metrics_server_kubelet_flag"), \
         patch.object(metrics, "_wait_for_deployment_ready", return_value=(1, 1, 0)), \
         patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"x"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        out = metrics.bootstrap_metrics_server()

    assert "https://internal.example/m.yaml" in out
    # The fetch URL passed to urlopen is the override.
    called_url = mock_urlopen.call_args.args[0]
    assert "internal.example" in called_url
