"""Tests for the new write helpers added in v0.6.0:

  - `create_namespace` (cluster-scoped)
  - `create_configmap` (data dict / raw YAML)
  - `create_secret` (data base64 / string_data plaintext)

`generic.apply_yaml` is the actual apply path; here we patch it to a
MagicMock and verify it was called with the expected manifest, plus
exercise validation paths independently.
"""
from __future__ import annotations

import base64
from unittest.mock import patch

import pytest
import yaml

from k8s_mcp.config import Settings
from k8s_mcp.tools import configmap, namespace, secret


@pytest.fixture(autouse=True)
def _clean_settings(monkeypatch):
    # Keep this module isolated from caller-provided runtime policy.
    # The project default permits writes; dedicated tests cover read-only mode.
    monkeypatch.delenv("K8S_MCP_READ_ONLY", raising=False)
    monkeypatch.delenv("K8S_MCP_NAMESPACE_ALLOWLIST", raising=False)
    from k8s_mcp.config import reset_settings_cache
    reset_settings_cache()
    yield
    reset_settings_cache()


# ---------- create_namespace ----------


def test_create_namespace_basic():
    with patch.object(namespace.generic, "apply_yaml",
                      return_value="Namespace/prod created") as m:
        result = namespace.create_namespace("prod")
    manifest = yaml.safe_load(m.call_args[0][0])
    assert manifest["apiVersion"] == "v1"
    assert manifest["kind"] == "Namespace"
    assert manifest["metadata"]["name"] == "prod"
    assert "spec" in manifest
    assert result == "Namespace/prod created"


def test_create_namespace_with_labels_and_annotations():
    with patch.object(namespace.generic, "apply_yaml") as m:
        namespace.create_namespace(
            "team-a",
            labels={"env": "prod", "team": "platform"},
            annotations={"contact": "platform@example.com"},
        )
    manifest = yaml.safe_load(m.call_args[0][0])
    md = manifest["metadata"]
    assert md["labels"] == {"env": "prod", "team": "platform"}
    assert md["annotations"] == {"contact": "platform@example.com"}


def test_create_namespace_rejects_invalid_name():
    with patch.object(namespace.generic, "apply_yaml") as m:
        with pytest.raises(ValueError, match="invalid namespace name"):
            namespace.create_namespace("Invalid-Name")
        with pytest.raises(ValueError, match="invalid namespace name"):
            namespace.create_namespace("")
    m.assert_not_called()


def test_create_namespace_rejects_read_only():
    with patch.object(namespace, "get_settings",
                      return_value=Settings(read_only=True)), \
         patch.object(namespace.generic, "apply_yaml") as m:
        with pytest.raises(PermissionError, match="read-only"):
            namespace.create_namespace("prod")
    m.assert_not_called()


def test_create_namespace_rejects_cluster_scoped_when_allowlist_set():
    s = Settings(read_only=False, namespace_allowlist=["default"])
    with patch.object(namespace, "get_settings", return_value=s), \
         patch.object(namespace.generic, "apply_yaml") as m:
        with pytest.raises(PermissionError, match="cluster-scoped"):
            namespace.create_namespace("prod")
    m.assert_not_called()


def test_create_namespace_rejects_invalid_labels():
    with patch.object(namespace.generic, "apply_yaml") as m:
        with pytest.raises(ValueError, match="invalid label key"):
            namespace.create_namespace("prod", labels={"with spaces": "v"})
    m.assert_not_called()


# ---------- create_configmap ----------


def test_create_configmap_from_data():
    with patch.object(configmap.generic, "apply_yaml") as m:
        result = configmap.create_configmap(
            "app-config", "default",
            data={"key1": "v1", "key2": "v2"},
        )
    manifest = yaml.safe_load(m.call_args[0][0])
    assert manifest["kind"] == "ConfigMap"
    assert manifest["metadata"]["name"] == "app-config"
    assert manifest["metadata"]["namespace"] == "default"
    assert manifest["data"] == {"key1": "v1", "key2": "v2"}
    assert result == m.return_value


def test_create_configmap_from_raw_yaml():
    raw = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n  namespace: y\nbinaryData:\n  foo: YmFy\n"
    with patch.object(configmap.generic, "apply_yaml", return_value="ConfigMap/x created") as m:
        result = configmap.create_configmap("x", "y", yaml_content=raw)
    assert m.call_args[0][0] == raw
    assert result == "ConfigMap/x created"


def test_create_configmap_requires_exactly_one_input():
    with patch.object(configmap.generic, "apply_yaml") as m:
        with pytest.raises(ValueError, match="exactly one"):
            configmap.create_configmap("x", "y")
        with pytest.raises(ValueError, match="exactly one"):
            configmap.create_configmap("x", "y",
                                       data={"k": "v"},
                                       yaml_content="apiVersion: v1\nkind: ConfigMap\n")
    m.assert_not_called()


def test_create_configmap_rejects_empty_data():
    with patch.object(configmap.generic, "apply_yaml") as m:
        with pytest.raises(ValueError, match="non-empty"):
            configmap.create_configmap("x", "y", data={})
    m.assert_not_called()


def test_create_configmap_rejects_read_only():
    s = Settings(read_only=True)
    with patch.object(configmap, "get_settings", return_value=s), \
         patch.object(configmap.generic, "apply_yaml") as m:
        with pytest.raises(PermissionError, match="read-only"):
            configmap.create_configmap("x", "default", data={"k": "v"})
    m.assert_not_called()


# ---------- create_secret ----------


def test_create_secret_string_data_auto_base64():
    with patch.object(secret.generic, "apply_yaml") as m:
        secret.create_secret("db", "default",
                             string_data={"password": "hunter2"})
    manifest = yaml.safe_load(m.call_args[0][0])
    assert manifest["kind"] == "Secret"
    assert manifest["type"] == "Opaque"
    assert manifest["data"]["password"] == base64.b64encode(b"hunter2").decode("ascii")


def test_create_secret_data_passthrough_when_already_base64():
    b64 = base64.b64encode(b"topsecret").decode("ascii")
    with patch.object(secret.generic, "apply_yaml") as m:
        secret.create_secret("db", "default", data={"password": b64})
    manifest = yaml.safe_load(m.call_args[0][0])
    assert manifest["data"]["password"] == b64


def test_create_secret_with_secret_type():
    with patch.object(secret.generic, "apply_yaml") as m:
        secret.create_secret("tls", "default",
                             string_data={"tls.crt": "x", "tls.key": "y"},
                             secret_type="kubernetes.io/tls")
    manifest = yaml.safe_load(m.call_args[0][0])
    assert manifest["type"] == "kubernetes.io/tls"
    assert "tls.crt" in manifest["data"]


def test_create_secret_rejects_empty_value():
    with patch.object(secret.generic, "apply_yaml") as m:
        with pytest.raises(ValueError, match="empty value"):
            secret.create_secret("db", "default",
                                 string_data={"password": ""})
    m.assert_not_called()


def test_create_secret_rejects_both_data_and_string_data():
    with patch.object(secret.generic, "apply_yaml") as m:
        with pytest.raises(ValueError, match="exactly one"):
            secret.create_secret("db", "default",
                                 data={"k": "YQ=="},
                                 string_data={"k": "a"})
    m.assert_not_called()


def test_create_secret_rejects_empty_dict():
    with patch.object(secret.generic, "apply_yaml") as m:
        with pytest.raises(ValueError, match="non-empty"):
            secret.create_secret("db", "default", data={})
        with pytest.raises(ValueError, match="non-empty"):
            secret.create_secret("db", "default", string_data={})
    m.assert_not_called()


def test_create_secret_rejects_read_only():
    s = Settings(read_only=True)
    with patch.object(secret, "get_settings", return_value=s), \
         patch.object(secret.generic, "apply_yaml") as m:
        with pytest.raises(PermissionError, match="read-only"):
            secret.create_secret("db", "default", string_data={"k": "v"})
    m.assert_not_called()


def test_create_secret_rejects_namespace_allowlist_violation():
    s = Settings(read_only=False, namespace_allowlist=["prod"])
    with patch.object(secret, "get_settings", return_value=s), \
         patch.object(secret.generic, "apply_yaml") as m:
        with pytest.raises(PermissionError, match="namespace allowlist|K8S_MCP_NAMESPACE_ALLOWLIST"):
            secret.create_secret("db", "default", string_data={"k": "v"})
    m.assert_not_called()
