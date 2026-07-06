"""Tests for the single-step `delete_resource` tool (v0.5.2+).

The previous two-step preview → confirm flow was removed in v0.5.2 — see
the module docstring in `src/k8s_mcp/tools/delete_tool.py` for the
threat-model rationale. Delete is now guarded only by:

  - `K8S_MCP_READ_ONLY` (global kill switch)
  - `K8S_MCP_NAMESPACE_ALLOWLIST` (per-namespace write scoping)

These tests pin the two safety guards and the happy-path execute.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from k8s_mcp.config import reset_settings_cache
from k8s_mcp.tools import delete_tool


@pytest.fixture(autouse=True)
def _clear():
    reset_settings_cache()
    yield
    reset_settings_cache()


def test_delete_read_only_rejected(monkeypatch):
    """Read-only mode raises PermissionError regardless of inputs."""
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "true")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="read-only"):
        delete_tool.delete_resource(kind="Pod", name="x", namespace="default")


def test_delete_blocked_by_namespace_allowlist(monkeypatch):
    """When allowlist is set, deletes outside it are refused — including
    cluster-scoped deletes (no namespace)."""
    monkeypatch.setenv("K8S_MCP_NAMESPACE_ALLOWLIST", "allowed")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="not allowed"):
        delete_tool.delete_resource(kind="Pod", name="x", namespace="other")


def test_delete_cluster_scoped_blocked_when_allowlist_set(monkeypatch):
    """Cluster-scoped delete (no namespace) is rejected when allowlist is
    on — otherwise the allowlist could be bypassed by writing cluster
    objects directly."""
    monkeypatch.setenv("K8S_MCP_NAMESPACE_ALLOWLIST", "default,app")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="not allowed"):
        delete_tool.delete_resource(kind="Namespace", name="rogue")


def test_delete_happy_path_namespaced(monkeypatch):
    """Namespaced delete calls resource.delete with the right kwargs."""
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    monkeypatch.setenv("K8S_MCP_NAMESPACE_ALLOWLIST", "default")
    reset_settings_cache()

    fake_resource = MagicMock()
    dc = MagicMock()
    dc.resources.get.return_value = fake_resource
    with patch.object(delete_tool, "_dyn_client", return_value=dc), \
         patch.object(delete_tool, "_api_version_for", return_value="v1"):
        out = delete_tool.delete_resource(
            kind="ConfigMap", name="cm1", namespace="default",
            grace_period_seconds=10,
        )

    fake_resource.delete.assert_called_once_with(
        name="cm1", namespace="default", grace_period_seconds=10,
    )
    assert out == {
        "deleted": True,
        "kind": "ConfigMap",
        "name": "cm1",
        "namespace": "default",
        "grace_period_seconds": 10,
    }


def test_delete_happy_path_cluster_scoped(monkeypatch):
    """Cluster-scoped delete (no namespace) works when allowlist is NOT set."""
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()

    fake_resource = MagicMock()
    dc = MagicMock()
    dc.resources.get.return_value = fake_resource
    with patch.object(delete_tool, "_dyn_client", return_value=dc), \
         patch.object(delete_tool, "_api_version_for", return_value="v1"):
        out = delete_tool.delete_resource(
            kind="Namespace", name="ns1", grace_period_seconds=15,
        )

    fake_resource.delete.assert_called_once_with(
        name="ns1", grace_period_seconds=15,
    )
    assert out["namespace"] is None


def test_delete_already_gone_raises_lookup(monkeypatch):
    """A NotFoundError from the apiserver surfaces as LookupError."""
    from kubernetes import dynamic
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()

    fake_resource = MagicMock()
    fake_resource.delete.side_effect = dynamic.exceptions.NotFoundError(
        MagicMock(status=404), "missing"
    )
    dc = MagicMock()
    dc.resources.get.return_value = fake_resource
    with patch.object(delete_tool, "_dyn_client", return_value=dc), \
         patch.object(delete_tool, "_api_version_for", return_value="v1"):
        with pytest.raises(LookupError, match="already gone"):
            delete_tool.delete_resource(
                kind="Pod", name="gone", namespace="default",
            )


def test_delete_unknown_kind_raises_value(monkeypatch):
    """An unknown kind (no matching resource in the dynamic client) → ValueError."""
    from kubernetes.dynamic.exceptions import ResourceNotFoundError
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()

    dc = MagicMock()
    dc.resources.get.side_effect = ResourceNotFoundError(MagicMock(), "nope")
    with patch.object(delete_tool, "_dyn_client", return_value=dc), \
         patch.object(delete_tool, "_api_version_for", return_value="v1"):
        with pytest.raises(ValueError, match="Unknown kind"):
            delete_tool.delete_resource(
                kind="FakeKind", name="x", namespace="default",
            )
