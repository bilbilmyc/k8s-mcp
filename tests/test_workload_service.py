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


# ---- create_job / create_cronjob manifest building -----------------------


def _capture_apply(monkeypatch, target_module):
    """Replace `generic.apply_yaml` on the target module with a recording fake
    that captures the YAML string passed in. Returns the captured list."""
    captured: list[str] = []

    def fake_apply(yaml_text: str) -> str:
        captured.append(yaml_text)
        return "ok"

    monkeypatch.setattr(target_module, "apply_yaml", fake_apply)
    return captured


def test_create_job_read_only_rejected(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "true")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="read-only"):
        workload.create_job(name="migrate", image="postgres:16")


def test_create_job_blocked_by_allowlist(monkeypatch):
    monkeypatch.setenv("K8S_MCP_NAMESPACE_ALLOWLIST", "allowed")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="not allowed"):
        workload.create_job(name="migrate", image="postgres:16", namespace="other")


def test_create_job_builds_manifest(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()
    captured = _capture_apply(monkeypatch, workload.generic)
    workload.create_job(
        name="migrate",
        image="postgres:16-alpine",
        namespace="app",
        command=["pg_dump", "-U", "postgres"],
        args=["-d", "mydb"],
        env={"PGHOST": "db"},
        resources={"requests": {"cpu": "100m", "memory": "256Mi"}},
        backoff_limit=3,
    )
    import yaml as _y
    manifest = _y.safe_load(captured[0])
    assert manifest["apiVersion"] == "batch/v1"
    assert manifest["kind"] == "Job"
    assert manifest["metadata"]["name"] == "migrate"
    assert manifest["metadata"]["namespace"] == "app"
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    assert container["name"] == "migrate"
    assert container["image"] == "postgres:16-alpine"
    assert container["command"] == ["pg_dump", "-U", "postgres"]
    assert container["args"] == ["-d", "mydb"]
    assert container["env"] == [{"name": "PGHOST", "value": "db"}]
    assert manifest["spec"]["template"]["spec"]["restartPolicy"] == "Never"
    assert manifest["spec"]["backoffLimit"] == 3


def test_create_job_default_restart_policy(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()
    captured = _capture_apply(monkeypatch, workload.generic)
    workload.create_job(name="tidy", image="busybox:1.36")
    import yaml as _y
    manifest = _y.safe_load(captured[0])
    assert manifest["spec"]["template"]["spec"]["restartPolicy"] == "Never"
    # backoffLimit omitted by default
    assert "backoffLimit" not in manifest["spec"]


def test_create_cronjob_read_only_rejected(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "true")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="read-only"):
        workload.create_cronjob(name="nightly", image="alpine:3", schedule="0 2 * * *")


def test_create_cronjob_blocked_by_allowlist(monkeypatch):
    monkeypatch.setenv("K8S_MCP_NAMESPACE_ALLOWLIST", "allowed")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="not allowed"):
        workload.create_cronjob(
            name="nightly", image="alpine:3", schedule="0 2 * * *", namespace="other",
        )


def test_create_cronjob_builds_manifest(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()
    captured = _capture_apply(monkeypatch, workload.generic)
    workload.create_cronjob(
        name="nightly",
        image="alpine:3",
        schedule="0 2 * * *",
        namespace="app",
        command=["sh", "-c", "echo hi"],
        env={"FOO": "bar"},
    )
    import yaml as _y
    manifest = _y.safe_load(captured[0])
    assert manifest["apiVersion"] == "batch/v1"
    assert manifest["kind"] == "CronJob"
    assert manifest["metadata"]["name"] == "nightly"
    assert manifest["metadata"]["namespace"] == "app"
    assert manifest["spec"]["schedule"] == "0 2 * * *"
    # Nested jobTemplate > spec > template > spec > containers
    container = manifest["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "alpine:3"
    assert container["command"] == ["sh", "-c", "echo hi"]
    assert container["env"] == [{"name": "FOO", "value": "bar"}]
    # CronJob default restartPolicy is OnFailure
    assert manifest["spec"]["jobTemplate"]["spec"]["template"]["spec"]["restartPolicy"] == "OnFailure"


def test_create_cronjob_default_restart_policy(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()
    captured = _capture_apply(monkeypatch, workload.generic)
    workload.create_cronjob(name="t", image="i", schedule="* * * * *")
    import yaml as _y
    manifest = _y.safe_load(captured[0])
    assert manifest["spec"]["jobTemplate"]["spec"]["template"]["spec"]["restartPolicy"] == "OnFailure"
