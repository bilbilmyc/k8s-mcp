"""Tests for `add_label` / `remove_label` — atomic label patch tools.

We monkeypatch `_dyn_client` to feed a fake resource whose `patch()`
captures the body + content_type so we can assert the patch shape.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from kubernetes.client.rest import ApiException
from kubernetes.dynamic.exceptions import ResourceNotFoundError

from k8s_mcp.config import reset_settings_cache
from k8s_mcp.tools import generic


@pytest.fixture(autouse=True)
def _settings():
    """Pin settings to writable + no allowlist so default tests focus on
    the patch shape, not the safety gates (those have dedicated tests)."""
    import os
    os.environ["K8S_MCP_READ_ONLY"] = "false"
    os.environ.pop("K8S_MCP_NAMESPACE_ALLOWLIST", None)
    reset_settings_cache()
    yield
    reset_settings_cache()


class _CapturingResource:
    """Records every patch() call so tests can assert body shape."""

    def __init__(self, kind: str = "Pod", raise_exc: Exception | None = None):
        self.kind = kind
        self.calls: list[dict] = []
        self._raise = raise_exc

    def patch(self, body=None, namespace=None, content_type=None, name=None, **kwargs):
        self.calls.append({
            "name": name,
            "namespace": namespace,
            "body": body,
            "content_type": content_type,
        })
        if self._raise is not None:
            raise self._raise
        return None


class _Dyn:
    def __init__(self, resource):
        self._resource = resource

    @property
    def resources(self):
        return _Resources(self._resource)


class _Resources:
    def __init__(self, resource):
        self._resource = resource

    def get(self, api_version=None, kind=None):
        if kind == "Missing":
            raise ResourceNotFoundError("missing")
        return self._resource


# ---------- add_label --------------------------------------------------------


def test_add_label_sends_json_patch_add():
    res = _CapturingResource()
    with patch.object(generic, "_dyn_client", return_value=_Dyn(res)):
        out = generic.add_label("Pod", "web-1", "app", "web", namespace="app")
    assert "✅ added label app=web" in out
    assert "Pod/app/web-1" in out
    assert len(res.calls) == 1
    call = res.calls[0]
    # JSON Patch shape — one op, add to /metadata/labels/<key>
    assert call["content_type"] == "application/json-patch+json"
    assert call["body"] == [{
        "op": "add",
        "path": "/metadata/labels/app",
        "value": "web",
    }]
    assert call["name"] == "web-1"
    assert call["namespace"] == "app"


def test_add_label_escapes_label_key_with_slash():
    """Label keys like `app.kubernetes.io/name` need `/` → `~1` per
    RFC 6901 so the JSON Patch path parses correctly."""
    res = _CapturingResource()
    with patch.object(generic, "_dyn_client", return_value=_Dyn(res)):
        generic.add_label("Pod", "p1", "app.kubernetes.io/name", "web")
    path = res.calls[0]["body"][0]["path"]
    assert path == "/metadata/labels/app.kubernetes.io~1name"


def test_add_label_escapes_label_key_with_tilde():
    """`~` must be escaped before `/` to avoid double-escape bugs."""
    res = _CapturingResource()
    with patch.object(generic, "_dyn_client", return_value=_Dyn(res)):
        generic.add_label("Pod", "p1", "weird~key", "v")
    path = res.calls[0]["body"][0]["path"]
    assert path == "/metadata/labels/weird~0key"


def test_add_label_rejects_empty_key():
    with pytest.raises(ValueError, match="non-empty"):
        generic.add_label("Pod", "p1", "", "v")


def test_add_label_rejects_non_string_value():
    with pytest.raises(ValueError, match="string"):
        generic.add_label("Pod", "p1", "k", 123)  # type: ignore[arg-type]


def test_add_label_rejects_in_read_only_mode():
    import os
    os.environ["K8S_MCP_READ_ONLY"] = "true"
    reset_settings_cache()
    with pytest.raises(PermissionError, match="read-only"):
        generic.add_label("Pod", "p1", "k", "v")
    os.environ["K8S_MCP_READ_ONLY"] = "false"
    reset_settings_cache()


def test_add_label_rejects_when_namespace_not_in_allowlist():
    import os
    os.environ["K8S_MCP_NAMESPACE_ALLOWLIST"] = "allowed"
    reset_settings_cache()
    with pytest.raises(PermissionError, match="not allowed"):
        generic.add_label("Pod", "p1", "k", "v", namespace="other")
    os.environ.pop("K8S_MCP_NAMESPACE_ALLOWLIST", None)
    reset_settings_cache()


def test_add_label_propagates_api_error():
    res = _CapturingResource(
        raise_exc=ApiException(status=404, reason="not found"),
    )
    with patch.object(generic, "_dyn_client", return_value=_Dyn(res)):
        with pytest.raises(RuntimeError, match="failed to add label"):
            generic.add_label("Pod", "ghost", "k", "v")


def test_add_label_cluster_scoped_kind_omits_namespace():
    """Cluster-scoped resources (e.g. Node) have no namespace — the
    patch call must not include a namespace kwarg."""
    res = _CapturingResource()
    with patch.object(generic, "_dyn_client", return_value=_Dyn(res)):
        generic.add_label("Node", "node-1", "team", "platform")
    assert res.calls[0]["namespace"] is None


# ---------- remove_label -----------------------------------------------------


def test_remove_label_sends_strategic_merge_with_null():
    """Strategic merge patch with `null` value removes the key —
    this is what `kubectl label foo bar-` does internally."""
    res = _CapturingResource()
    with patch.object(generic, "_dyn_client", return_value=_Dyn(res)):
        out = generic.remove_label("Pod", "web-1", "old", namespace="app")
    assert "✅ removed label old" in out
    assert "Pod/app/web-1" in out
    assert len(res.calls) == 1
    call = res.calls[0]
    assert call["content_type"] is None  # default = strategic merge patch
    assert call["body"] == {"metadata": {"labels": {"old": None}}}


def test_remove_label_cluster_scoped_kind():
    res = _CapturingResource()
    with patch.object(generic, "_dyn_client", return_value=_Dyn(res)):
        generic.remove_label("Node", "n1", "old")
    assert res.calls[0]["namespace"] is None
    assert res.calls[0]["body"] == {"metadata": {"labels": {"old": None}}}


def test_remove_label_rejects_empty_key():
    with pytest.raises(ValueError, match="non-empty"):
        generic.remove_label("Pod", "p1", "")


def test_remove_label_rejects_in_read_only_mode():
    import os
    os.environ["K8S_MCP_READ_ONLY"] = "true"
    reset_settings_cache()
    with pytest.raises(PermissionError, match="read-only"):
        generic.remove_label("Pod", "p1", "k")
    os.environ["K8S_MCP_READ_ONLY"] = "false"
    reset_settings_cache()


def test_remove_label_rejects_when_namespace_not_in_allowlist():
    import os
    os.environ["K8S_MCP_NAMESPACE_ALLOWLIST"] = "allowed"
    reset_settings_cache()
    with pytest.raises(PermissionError, match="not allowed"):
        generic.remove_label("Pod", "p1", "k", namespace="other")
    os.environ.pop("K8S_MCP_NAMESPACE_ALLOWLIST", None)
    reset_settings_cache()


def test_remove_label_propagates_api_error_with_context():
    """A non-404 API error from the apiserver (e.g. RBAC forbidden) must
    surface as RuntimeError with the resource locator in the message —
    agents need this to diagnose the failure."""
    res = _CapturingResource(
        raise_exc=ApiException(status=403, reason="forbidden"),
    )
    with patch.object(generic, "_dyn_client", return_value=_Dyn(res)):
        with pytest.raises(RuntimeError) as excinfo:
            generic.remove_label("Pod", "p1", "k", namespace="app")
    msg = str(excinfo.value)
    assert "remove label k" in msg
    assert "Pod/app/p1" in msg
    assert "403" in msg


def test_jsonpatch_escape_unit():
    """Unit test the escape helper directly — both `~` and `/` must
    be escaped, with `~` first to avoid double-escape (`~1` → `~01`)."""
    assert generic._jsonpatch_escape("simple") == "simple"
    assert generic._jsonpatch_escape("a.b.c") == "a.b.c"
    assert generic._jsonpatch_escape("a/b") == "a~1b"
    assert generic._jsonpatch_escape("a~b") == "a~0b"
    assert generic._jsonpatch_escape("a~/b") == "a~0~1b"
