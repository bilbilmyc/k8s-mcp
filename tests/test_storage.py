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


# ---------- create_pvc volume_name + hostPath hint -------------------------


def test_create_pvc_with_volume_name_sets_spec(monkeypatch):
    """When the caller pins a PVC to a specific PV (hostPath / local dev
    clusters), the manifest must carry spec.volumeName."""
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()
    captured: list[str] = []
    monkeypatch.setattr(storage.generic, "apply_yaml",
                        lambda y: captured.append(y) or "ok")
    # PV lookup should NOT happen when there's no hostPath — fake it to
    # raise, and assert we never call it.
    def boom(*a, **kw):
        raise AssertionError("PV should not be looked up for non-hostPath PVs")
    monkeypatch.setattr(storage.generic, "get_resource", boom)

    out = storage.create_pvc(name="data", namespace="app", size="5Gi",
                             volume_name="my-pv")
    assert "ok" in out
    sent = captured[0]
    assert "volumeName: my-pv" in sent
    # No hostPath → no hint
    assert "mkdir" not in out


def test_create_pvc_hostpath_pv_emits_mkdir_hint(monkeypatch):
    """When the bound PV is hostPath, the tool must surface the kubelet-
    does-not-mkdir caveat + the SSH command. Otherwise the agent (or user)
    schedules a Pod that immediately hits FailedMount."""
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()
    monkeypatch.setattr(storage.generic, "apply_yaml",
                        lambda y: "PVC created")

    pv = {
        "metadata": {"name": "pgsql-pv-local"},
        "spec": {
            "hostPath": {"path": "/data/k8s/pgsql-sts",
                         "type": "DirectoryOrCreate"},
            "nodeAffinity": {
                "required": {
                    "nodeSelectorTerms": [{
                        "matchExpressions": [{
                            "key": "kubernetes.io/hostname",
                            "operator": "In",
                            "values": ["deploy"],
                        }],
                    }],
                },
            },
        },
    }
    monkeypatch.setattr(storage.generic, "get_resource",
                        lambda **kw: pv)

    out = storage.create_pvc(name="pgsql-data-pvc", namespace="default",
                             size="10Gi", volume_name="pgsql-pv-local")
    assert "PVC created" in out
    # Hint must literally promote the action + the host path + the node
    assert "mkdir" in out
    assert "/data/k8s/pgsql-sts" in out
    assert "deploy" in out
    assert "kubelet" in out
    assert "validate_pv_hostpath_paths" in out  # escape hatch named in hint


def test_create_pvc_non_hostpath_pv_skips_hint(monkeypatch):
    """If volume_name is set but the PV is NFS / cloud-disk, no hostPath
    warning — there's nothing the operator needs to do on a node."""
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()
    monkeypatch.setattr(storage.generic, "apply_yaml",
                        lambda y: "PVC created")
    pv = {
        "metadata": {"name": "nfs-pv"},
        "spec": {
            "nfs": {"server": "10.0.0.1", "path": "/exports"},
        },
    }
    monkeypatch.setattr(storage.generic, "get_resource",
                        lambda **kw: pv)

    out = storage.create_pvc(name="d", namespace="app", size="1Gi",
                             volume_name="nfs-pv")
    assert "PVC created" in out
    assert "mkdir" not in out


def test_create_pvc_hostpath_pv_lookup_failure_silently_skips_hint(monkeypatch):
    """If the PV lookup errors out (PV not yet created, race condition,
    RBAC), don't fail the PVC creation — just skip the hint. The create
    itself is the user's primary intent."""
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()
    monkeypatch.setattr(storage.generic, "apply_yaml",
                        lambda y: "PVC created")
    monkeypatch.setattr(storage.generic, "get_resource",
                        lambda **kw: (_ for _ in ()).throw(
                            RuntimeError("PV not found")))

    out = storage.create_pvc(name="d", namespace="app", size="1Gi",
                             volume_name="ghost-pv")
    assert "PVC created" in out
    # No exception, no false hint
    assert "mkdir" not in out


# ---------- validate_pv_hostpath_paths -------------------------------------


def _fake_pv_obj(name, path, node=None, typ="DirectoryOrCreate", bound=False):
    obj = {
        "metadata": {"name": name},
        "spec": {
            "hostPath": {"path": path, "type": typ},
            "capacity": {"storage": "10Gi"},
        },
    }
    if node:
        obj["spec"]["nodeAffinity"] = {
            "required": {
                "nodeSelectorTerms": [{
                    "matchExpressions": [{
                        "key": "kubernetes.io/hostname",
                        "operator": "In",
                        "values": [node],
                    }],
                }],
            },
        }
    if bound:
        obj["spec"]["claimRef"] = {"namespace": "default", "name": f"{name}-claim"}
    return obj


def test_validate_pv_hostpath_paths_empty_list_reports_cleanly(monkeypatch):
    """No hostPath PVs → message should point at OTHER possible causes
    (no StorageClass, capacity, selectors), not at host paths."""
    monkeypatch.setattr(storage, "_list_hostpath_pvs", lambda: [])
    out = storage.validate_pv_hostpath_paths()
    assert "No hostPath PVs" in out
    # hint at non-hostPath causes
    assert "StorageClass" in out or "Pending" in out


def test_validate_pv_hostpath_paths_lists_each_pv_with_ssh_cmd(monkeypatch):
    pv_records = [
        {
            "name": "pgsql-pv-local",
            "capacity": "10Gi",
            "claim_ns": "default",
            "claim_name": "pgsql-data-pvc",
            "path": "/data/k8s/pgsql-sts",
            "type": "DirectoryOrCreate",
            "node": "deploy",
        },
        {
            "name": "redis-pv-local",
            "capacity": "5Gi",
            "claim_ns": "",
            "claim_name": "",
            "path": "/data/k8s/redis",
            "type": "DirectoryOrCreate",
            "node": "deploy",
        },
    ]
    monkeypatch.setattr(storage, "_list_hostpath_pvs", lambda: pv_records)
    out = storage.validate_pv_hostpath_paths()
    assert "Found 2 hostPath PV" in out
    assert "pgsql-pv-local" in out
    assert "redis-pv-local" in out
    assert "/data/k8s/pgsql-sts" in out
    assert "ssh deploy" in out
    assert "ls -ld" in out
    assert "mkdir -p" in out
    # bound PVC is shown
    assert "bound to default/pgsql-data-pvc" in out
    # unbound PV doesn't claim a binding
    assert "redis-pv-local" in out
    # redis has empty claim → no "bound to" line near it (acceptable: just
    # assert the name appears; the format places "bound to" only when set)


def test_validate_pv_hostpath_paths_warns_on_non_directory_or_create(monkeypatch):
    """If hostPath.type is e.g. Directory (NOT DirectoryOrCreate), the
    directory must already exist — the report must call this out so the
    operator doesn't assume `mkdir` is enough."""
    pv_records = [
        {
            "name": "strict-pv",
            "capacity": "1Gi",
            "claim_ns": "",
            "claim_name": "",
            "path": "/data/strict",
            "type": "Directory",  # kubelet won't create
            "node": "deploy",
        },
    ]
    monkeypatch.setattr(storage, "_list_hostpath_pvs", lambda: pv_records)
    out = storage.validate_pv_hostpath_paths()
    assert "DirectoryOrCreate" in out
    assert "must already exist" in out


def test_validate_pv_hostpath_paths_handles_unknown_node(monkeypatch):
    """When nodeAffinity is absent AND PV is unbound, we can't tell the
    operator which node to ssh to. Surface 'unknown' rather than crashing
    or silently dropping the entry."""
    pv_records = [
        {
            "name": "floating-pv",
            "capacity": "1Gi",
            "claim_ns": "",
            "claim_name": "",
            "path": "/data/x",
            "type": "DirectoryOrCreate",
            "node": "",  # unknown
        },
    ]
    monkeypatch.setattr(storage, "_list_hostpath_pvs", lambda: pv_records)
    out = storage.validate_pv_hostpath_paths()
    assert "floating-pv" in out
    assert "unknown" in out
    # ssh command must still render without crashing
    assert "ssh" in out


def test_list_hostpath_pvs_filters_non_hostpath(monkeypatch):
    """End-to-end: _list_hostpath_pvs uses generic._dyn_client() and
    filters by spec.hostPath presence. nfs / awsElasticBlockStore PVs
    must be dropped."""
    pvs = [
        _fake_pv_obj("pgsql-pv-local", "/data/pgsql", node="deploy"),
        {"metadata": {"name": "nfs-pv"},
         "spec": {"nfs": {"server": "10.0.0.1", "path": "/x"}}},
        {"metadata": {"name": "ebs-pv"},
         "spec": {"awsElasticBlockStore": {"volumeID": "vol-1"}}},
    ]

    class _FakeList:
        def __init__(self, items): self.items = items
    class _FakeResource:
        def get(self): return _FakeList(pvs)
    class _FakeDC:
        def resources(self): ...  # not used
    monkeypatch.setattr(storage.generic, "_dyn_client", lambda: _FakeDC())
    monkeypatch.setattr(storage.generic, "_resource_for_kind",
                        lambda dc, kind, api_version=None: _FakeResource())

    out = storage._list_hostpath_pvs()
    names = [r["name"] for r in out]
    assert names == ["pgsql-pv-local"]
    assert out[0]["path"] == "/data/pgsql"
    assert out[0]["node"] == "deploy"
