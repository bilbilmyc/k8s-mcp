"""Tests for the node_ops module extensions added in v0.6.0:

  - `list_nodes` (table view of cluster Nodes)
  - `label_node` / `unlabel_node` (atomic single-label ops)
  - `taint_node` / `untaint_node` (atomic single-taint ops)

Pure validation + apiserver-shape tests; the apiserver is mocked with
fake `V1Node` objects so the rendering / patch-body construction is
exercised without a real cluster.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException

from k8s_mcp.config import Settings
from k8s_mcp.tools import node_ops


@pytest.fixture(autouse=True)
def _clean_settings(monkeypatch):
    monkeypatch.delenv("K8S_MCP_READ_ONLY", raising=False)
    monkeypatch.delenv("K8S_MCP_NAMESPACE_ALLOWLIST", raising=False)
    from k8s_mcp.config import reset_settings_cache
    reset_settings_cache()
    yield
    reset_settings_cache()


def _mock_addr(typ: str, addr: str) -> MagicMock:
    a = MagicMock()
    a.type = typ
    a.address = addr
    return a


def _mock_taint(key: str, value: str, effect: str) -> MagicMock:
    t = MagicMock()
    t.key = key
    t.value = value
    t.effect = effect
    return t


def _make_node(name, *, role="worker", schedulable=True, taints=None,
               addresses=None, status="True"):
    n = MagicMock()
    md = MagicMock()
    md.name = name
    md.creation_timestamp = None
    md.labels = {"node-role.kubernetes.io/" + role: ""}
    n.metadata = md
    spec = MagicMock()
    spec.unschedulable = not schedulable
    spec.taints = [_mock_taint(k, v, eff) for k, v, eff in (taints or [])]
    n.spec = spec
    n.status.conditions = []
    cond = MagicMock()
    cond.type = "Ready"
    cond.status = status
    n.status.conditions.append(cond)
    n.status.addresses = [
        _mock_addr(typ, addr) for typ, addr in (addresses or [("InternalIP", "10.0.0.1")])
    ]
    return n


# ---------- validation helpers ----------


def test_parse_taint_spec_full():
    assert node_ops._parse_taint_spec("k=v:NoSchedule") == ("k", "v", "NoSchedule")


def test_parse_taint_spec_no_value():
    assert node_ops._parse_taint_spec("k:NoExecute") == ("k", "", "NoExecute")


def test_parse_taint_spec_bad_effect():
    with pytest.raises(ValueError, match="invalid taint effect"):
        node_ops._parse_taint_spec("k=v:BogusEffect")


def test_parse_taint_spec_missing_colon():
    with pytest.raises(ValueError, match="invalid taint spec"):
        node_ops._parse_taint_spec("k=v")


def test_parse_taint_spec_bad_key():
    with pytest.raises(ValueError, match="invalid taint key"):
        node_ops._parse_taint_spec("INVALID_KEY=v:NoSchedule")


def test_validate_label_value_rejects_bad():
    with pytest.raises(ValueError, match="invalid label value"):
        node_ops._validate_label_value("has spaces")


def test_validate_node_name_rejects_uppercase():
    with pytest.raises(ValueError, match="invalid node name"):
        node_ops._validate_node_name("MyNode")


def test_jsonpatch_escape_slash_and_tilde():
    assert node_ops._jsonpatch_escape("a/b~c") == "a~1b~0c"


# ---------- list_nodes ----------


def test_list_nodes_renders_columns():
    api = MagicMock()
    api.list_node.return_value.items = [
        _make_node("node-a", role="worker"),
        _make_node("node-b", role="control-plane",
                   taints=[("dedicated", "ml", "NoSchedule")]),
    ]
    with patch.object(node_ops, "_core_v1", return_value=api):
        out = node_ops.list_nodes()
    assert "NAME" in out and "STATUS" in out and "TAINT_SUMMARY" in out
    assert "node-a" in out and "node-b" in out
    assert "worker" in out and "control-plane" in out
    assert "dedicated=ml:NoSchedule" in out


def test_list_nodes_filters_unschedulable():
    api = MagicMock()
    api.list_node.return_value.items = [
        _make_node("node-a", schedulable=True),
        _make_node("node-b", schedulable=False),
    ]
    with patch.object(node_ops, "_core_v1", return_value=api):
        out = node_ops.list_nodes(include_unschedulable=False)
    assert "node-a" in out
    assert "node-b" not in out


def test_list_nodes_empty_returns_notice():
    api = MagicMock()
    api.list_node.return_value.items = []
    with patch.object(node_ops, "_core_v1", return_value=api):
        out = node_ops.list_nodes(label_selector="x=y")
    assert "(no Nodes matched the selector)" in out


# ---------- label_node / unlabel_node ----------


def test_label_node_uses_jsonpatch_and_escapes():
    api = MagicMock()
    with patch.object(node_ops, "_core_v1", return_value=api), \
         patch.object(node_ops, "get_settings", return_value=Settings(read_only=False)):
        result = node_ops.label_node("node-a", "a/b", "v1")
    assert api.patch_node.call_count == 1
    args, kwargs = api.patch_node.call_args
    body = args[1]
    assert body == [{
        "op": "add",
        "path": "/metadata/labels/a~1b",
        "value": "v1",
    }]
    assert "application/json-patch+json" in kwargs.get("content_type", "")
    assert "a/b=v1" in result


def test_label_node_rejects_read_only():
    api = MagicMock()
    with patch.object(node_ops, "_core_v1", return_value=api), \
         patch.object(node_ops, "get_settings", return_value=Settings(read_only=True)):
        with pytest.raises(PermissionError, match="read-only"):
            node_ops.label_node("node-a", "k", "v")
    api.patch_node.assert_not_called()


def test_label_node_rejects_cluster_scoped_when_allowlist_set():
    api = MagicMock()
    settings = Settings(read_only=False, namespace_allowlist=["default"])
    with patch.object(node_ops, "_core_v1", return_value=api), \
         patch.object(node_ops, "get_settings", return_value=settings):
        with pytest.raises(PermissionError, match="cluster-scoped"):
            node_ops.label_node("node-a", "k", "v")


def test_unlabel_node_idempotent_on_missing():
    api = MagicMock()
    err = ApiException(status=422, reason="Unprocessable Entity")
    api.patch_node.side_effect = err
    with patch.object(node_ops, "_core_v1", return_value=api), \
         patch.object(node_ops, "get_settings", return_value=Settings(read_only=False)):
        result = node_ops.unlabel_node("node-a", "k")
    assert "no-op" in result


def test_unlabel_node_raises_on_404():
    api = MagicMock()
    api.patch_node.side_effect = ApiException(status=404, reason="Not Found")
    with patch.object(node_ops, "_core_v1", return_value=api), \
         patch.object(node_ops, "get_settings", return_value=Settings(read_only=False)):
        with pytest.raises(LookupError, match="not found"):
            node_ops.unlabel_node("node-a", "k")


# ---------- taint_node / untaint_node ----------


def test_taint_node_appends_to_spec_taints():
    api = MagicMock()
    with patch.object(node_ops, "_core_v1", return_value=api), \
         patch.object(node_ops, "get_settings", return_value=Settings(read_only=False)):
        result = node_ops.taint_node("node-a", "dedicated=ml:NoSchedule")
    body = api.patch_node.call_args[0][1]
    assert body == [{
        "op": "add",
        "path": "/spec/taints/-",
        "value": {"key": "dedicated", "value": "ml", "effect": "NoSchedule"},
    }]
    assert "dedicated=ml:NoSchedule" in result


def test_untaint_node_specific_match():
    api = MagicMock()
    api.read_node.return_value.spec.taints = [
        _mock_taint("dedicated", "ml", "NoSchedule"),
        _mock_taint("old", "x", "NoSchedule"),
    ]
    with patch.object(node_ops, "_core_v1", return_value=api), \
         patch.object(node_ops, "get_settings", return_value=Settings(read_only=False)):
        result = node_ops.untaint_node("node-a", "dedicated=ml:NoSchedule")
    body = api.patch_node.call_args[0][1]
    assert body == [{"op": "remove", "path": "/spec/taints/0"}]
    assert "removed" in result


def test_untaint_node_noop_when_absent():
    api = MagicMock()
    api.read_node.return_value.spec.taints = []
    with patch.object(node_ops, "_core_v1", return_value=api), \
         patch.object(node_ops, "get_settings", return_value=Settings(read_only=False)):
        result = node_ops.untaint_node("node-a", "missing:NoSchedule")
    assert "no-op" in result
    api.patch_node.assert_not_called()


def test_untaint_node_wipe_all():
    api = MagicMock()
    with patch.object(node_ops, "_core_v1", return_value=api), \
         patch.object(node_ops, "get_settings", return_value=Settings(read_only=False)):
        result = node_ops.untaint_node("node-a")
    args, kwargs = api.patch_node.call_args
    assert args[1] == {"spec": {"taints": []}}
    assert "application/merge-patch+json" in kwargs.get("content_type", "")
    assert "all taints removed" in result


# ---------- read_only path (cordon_node unchanged behaviour) ----------


def test_cordon_node_unchanged():
    api = MagicMock()
    with patch.object(node_ops, "_core_v1", return_value=api), \
         patch.object(node_ops, "get_settings", return_value=Settings(read_only=False)):
        out = node_ops.cordon_node("node-a")
    api.patch_node.assert_called_once_with("node-a", {"spec": {"unschedulable": True}})
    assert "cordoned" in out
