"""Tests for workload/service/configmap safety guards."""
from __future__ import annotations

import pytest

from k8s_mcp.config import reset_settings_cache
from k8s_mcp.tools import configmap, service, workload


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_settings_cache()
    yield
    reset_settings_cache()


# ---- read-only guards -------------------------------------------------------


def test_create_deployment_read_only_rejected(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "true")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="read-only"):
        workload.create_deployment(name="x", image="nginx")


def test_create_service_read_only_rejected(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "true")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="read-only"):
        service.create_service(
            name="x", namespace="default",
            selector={"app": "x"}, ports=[{"port": 80, "targetPort": 80}],
        )


def test_create_ingress_read_only_rejected(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "true")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="read-only"):
        service.create_ingress(
            name="x", namespace="default",
            rules=[{"service_name": "svc", "service_port": 80}],
        )


def test_update_configmap_read_only_rejected(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "true")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="read-only"):
        configmap.update_configmap(name="x", namespace="default", data={"k": "v"})


# ---- namespace allowlist guards --------------------------------------------


def test_create_deployment_blocked_by_allowlist(monkeypatch):
    monkeypatch.setenv("K8S_MCP_NAMESPACE_ALLOWLIST", "allowed")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="not allowed"):
        workload.create_deployment(name="x", image="nginx", namespace="other")


def test_create_service_blocked_by_allowlist(monkeypatch):
    monkeypatch.setenv("K8S_MCP_NAMESPACE_ALLOWLIST", "allowed")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="not allowed"):
        service.create_service(
            name="x", namespace="other",
            selector={"app": "x"}, ports=[{"port": 80, "targetPort": 80}],
        )


def test_update_configmap_blocked_by_allowlist(monkeypatch):
    monkeypatch.setenv("K8S_MCP_NAMESPACE_ALLOWLIST", "allowed")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="not allowed"):
        configmap.update_configmap(name="x", namespace="other", data={"k": "v"})


# ---- input validation ------------------------------------------------------


def test_create_service_invalid_type(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()
    with pytest.raises(ValueError, match="Unsupported service_type"):
        service.create_service(
            name="x", namespace="default",
            selector={"app": "x"},
            ports=[{"port": 80, "targetPort": 80}],
            service_type="ExternalName",
        )


def test_scale_workload_unsupported_kind(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()
    with pytest.raises(ValueError, match="Unsupported kind"):
        workload.scale_workload(kind="Pod", name="x", namespace="default", replicas=3)


def test_set_image_unsupported_kind(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()
    with pytest.raises(ValueError, match="Unsupported kind"):
        workload.set_image(kind="Job", name="x", namespace="default", container="c", image="nginx:1.25")


def test_restart_workload_unsupported_kind(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()
    with pytest.raises(ValueError, match="Unsupported kind"):
        workload.restart_workload(kind="DaemonSet", name="x", namespace="default")
