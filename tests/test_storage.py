"""Tests for storage tools (create_pvc + bootstrap_local_path_provisioner)."""
from __future__ import annotations

import pytest

from k8s_mcp.config import reset_settings_cache
from k8s_mcp.tools import storage


@pytest.fixture(autouse=True)
def _clear():
    reset_settings_cache()
    storage._manifest_cache = None  # reset module-level cache between tests
    yield
    reset_settings_cache()


# A fake manifest that looks like Rancher's. The exact default-annotation
# form matters — the tool greps it to flip set_as_default behavior. Real
# Rancher ships YAML, so we use YAML here (not JSON-encoded strings).
_FAKE_MANIFEST = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: local-path-provisioner-config
  namespace: local-path-storage
data: {}
---
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: local-path
  annotations:
    storageclass.kubernetes.io/is-default-class: "true"
provisioner: rancher.io/local-path
volumeBindingMode: WaitForFirstConsumer
"""


class _FakeResponse:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_urlopen(monkeypatch, text):
    def fake_urlopen(req, *args, **kwargs):
        return _FakeResponse(text)
    monkeypatch.setattr(storage.urllib.request, "urlopen", fake_urlopen)


# ---------- create_pvc -------------------------------------------------------


def test_create_pvc_basic(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()
    captured: list[str] = []

    def fake_apply(y):
        captured.append(y)
        return "PVC created"

    monkeypatch.setattr(storage.generic, "apply_yaml", fake_apply)
    out = storage.create_pvc(name="data", namespace="app", size="5Gi")
    assert "PVC created" in out
    assert len(captured) == 1
    assert "name: data" in captured[0]
    sent = captured[0]
    # Size renders under storage field, not size.
    assert "name: data" in sent
    assert "storage: 5Gi" in sent
    assert "ReadWriteOnce" in sent


def test_create_pvc_respects_storage_class(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()
    captured: list[str] = []
    monkeypatch.setattr(storage.generic, "apply_yaml",
                        lambda y: captured.append(y) or "ok")
    storage.create_pvc(name="data", namespace="app", size="1Gi",
                       storage_class="local-path")
    assert "local-path" in captured[0]


def test_create_pvc_read_only_rejected(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "true")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="read-only"):
        storage.create_pvc(name="x", namespace="app", size="1Gi")


# ---------- bootstrap_local_path_provisioner ---------------------------------


def test_bootstrap_applies_manifest_and_returns_hint(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()
    _patch_urlopen(monkeypatch, _FAKE_MANIFEST)
    applied: list[str] = []
    monkeypatch.setattr(storage.generic, "apply_yaml",
                        lambda y: applied.append(y) or "applied")
    out = storage.bootstrap_local_path_provisioner()
    assert "applied" in out
    assert len(applied) == 1
    # hint must mention usage
    assert "local-path" in out
    assert "storage_class_name" in out


def test_bootstrap_strips_default_annotation_when_disabled(monkeypatch):
    """set_as_default=False flips the annotation so a coexisting default
    SC stays default."""
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()
    _patch_urlopen(monkeypatch, _FAKE_MANIFEST)
    applied: list[str] = []
    monkeypatch.setattr(storage.generic, "apply_yaml",
                        lambda y: applied.append(y) or "applied")
    storage.bootstrap_local_path_provisioner(set_as_default=False)
    sent = applied[0]
    # True → False, never leave the SC default-eligible by accident.
    assert '"true"' not in sent
    assert '"false"' in sent


def test_bootstrap_keeps_default_annotation_by_default(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()
    _patch_urlopen(monkeypatch, _FAKE_MANIFEST)
    applied: list[str] = []
    monkeypatch.setattr(storage.generic, "apply_yaml",
                        lambda y: applied.append(y) or "applied")
    storage.bootstrap_local_path_provisioner()
    sent = applied[0]
    assert '"true"' in sent


def test_bootstrap_dry_run_returns_manifest_without_applying(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()
    _patch_urlopen(monkeypatch, _FAKE_MANIFEST)
    applied: list[str] = []
    monkeypatch.setattr(storage.generic, "apply_yaml",
                        lambda y: applied.append(y) or "applied")
    out = storage.bootstrap_local_path_provisioner(apply_immediately=False)
    assert "NOT applied" in out
    assert "StorageClass" in out
    # apply_yaml must NOT be called
    assert applied == []


def test_bootstrap_read_only_rejected(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "true")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="read-only"):
        storage.bootstrap_local_path_provisioner()


def test_bootstrap_caches_manifest_within_session(monkeypatch):
    """Manifest is fetched once and cached for the rest of the MCP
    session (avoids re-fetching when the agent issues multiple
    bootstrap calls or restart-clobbers state)."""
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()
    fetch_count = {"n": 0}

    def counting_urlopen(*args, **kwargs):
        fetch_count["n"] += 1
        return _FakeResponse(_FAKE_MANIFEST)

    monkeypatch.setattr(storage.urllib.request, "urlopen", counting_urlopen)
    monkeypatch.setattr(storage.generic, "apply_yaml",
                        lambda y: "applied")

    storage.bootstrap_local_path_provisioner()
    storage.bootstrap_local_path_provisioner()
    storage.bootstrap_local_path_provisioner()

    assert fetch_count["n"] == 1


def test_bootstrap_url_failure_returns_actionable_error(monkeypatch):
    """If the URL fetch fails (air-gapped cluster), the error must point
    the user at K8S_MCP_LOCAL_PATH_PROVISIONER_URL override — and at the
    manual kubectl apply fallback."""
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()

    def boom(*args, **kwargs):
        import urllib.error
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(storage.urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError) as ei:
        storage.bootstrap_local_path_provisioner()
    msg = str(ei.value)
    assert "K8S_MCP_LOCAL_PATH_PROVISIONER_URL" in msg
    assert "kubectl apply" in msg
