"""Tests for kubectl-top equivalents (top_pods / top_nodes).

The non-trivial behavior is the **error path** when metrics-server is missing
— the error message must redirect the agent to the Prometheus fallback path,
otherwise the agent fixates on "install metrics-server" and never reaches
for find_prometheus_service() + prometheus_query().
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException

from k8s_mcp.config import reset_settings_cache
from k8s_mcp.tools import metrics


@pytest.fixture(autouse=True)
def _clear():
    reset_settings_cache()
    yield
    reset_settings_cache()


class _FakeCustomObjectsApi:
    """Returns a 404 ApiException to simulate missing metrics-server."""

    def list_cluster_custom_object(self, *args, **kwargs):
        raise ApiException(status=404, reason="Not Found")

    def list_namespaced_custom_object(self, *args, **kwargs):
        raise ApiException(status=404, reason="Not Found")


def test_top_pods_error_mentions_prometheus_fallback():
    """When metrics-server is missing, the error must literally name
    find_prometheus_service() and prometheus_query() — otherwise the
    agent fixates on install and never tries Prometheus."""
    fake_api = MagicMock()
    fake_api.return_value = _FakeCustomObjectsApi()
    with patch.object(metrics, "_custom_objects_api", fake_api):
        with pytest.raises(RuntimeError) as ei:
            metrics.top_pods(namespace="default")
    msg = str(ei.value)
    # The fallback path must literally surface in the error so the agent
    # picks it up on the next turn.
    assert "find_prometheus_service()" in msg
    assert "prometheus_query(" in msg
    assert "metrics-server" in msg  # the original hint stays


def test_top_nodes_error_mentions_prometheus_fallback():
    """Same as top_pods but for node-level metrics."""
    fake_api = MagicMock()
    fake_api.return_value = _FakeCustomObjectsApi()
    with patch.object(metrics, "_custom_objects_api", fake_api):
        with pytest.raises(RuntimeError) as ei:
            metrics.top_nodes()
    msg = str(ei.value)
    assert "find_prometheus_service()" in msg
    assert "prometheus_query(" in msg
