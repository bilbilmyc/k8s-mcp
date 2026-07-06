"""Tests for generic tools' safety logic (read_only, namespace allowlist).

The DynamicClient calls themselves require a live cluster; here we exercise
the guards that run before any API call.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from kubernetes.dynamic.exceptions import (
    ResourceNotFoundError,
    ResourceNotUniqueError,
)

from k8s_mcp.config import Settings, reset_settings_cache
from k8s_mcp.tools import generic


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_settings_cache()
    yield
    reset_settings_cache()


def test_apply_yaml_rejects_in_read_only_mode():
    Settings(_env_file=None, read_only=True)  # noqa - just force a settings re-read
    # override the get_settings() cache via monkeypatched env
    import os
    os.environ["K8S_MCP_READ_ONLY"] = "true"
    reset_settings_cache()
    with pytest.raises(PermissionError, match="read-only"):
        generic.apply_yaml("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n")


def test_apply_yaml_rejects_when_namespace_not_in_allowlist():
    import os
    os.environ["K8S_MCP_READ_ONLY"] = "false"
    os.environ["K8S_MCP_NAMESPACE_ALLOWLIST"] = "allowed"
    reset_settings_cache()
    yaml = (
        "apiVersion: v1\n"
        "kind: ConfigMap\n"
        "metadata:\n"
        "  name: x\n"
        "  namespace: other\n"
    )
    with pytest.raises(PermissionError, match="not allowed"):
        generic.apply_yaml(yaml)


def test_apply_yaml_accepts_when_namespace_in_allowlist(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    monkeypatch.setenv("K8S_MCP_NAMESPACE_ALLOWLIST", "allowed")
    reset_settings_cache()

    fake_resource = _FakeResource()
    fake_dyn = _FakeDynClient(resources={"ConfigMap": fake_resource})

    with patch.object(generic, "_dyn_client", return_value=fake_dyn):
        out = generic.apply_yaml(
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n  namespace: allowed\n"
        )
    # FakeResource.get returns success → apply path is "configured (patched)"
    assert "ConfigMap/x" in out
    assert ("created" in out) or ("configured" in out)


def test_apply_yaml_handles_multi_doc(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()
    cm = _FakeResource()
    dep = _FakeResource()
    fake_dyn = _FakeDynClient(resources={"ConfigMap": cm, "Deployment": dep})

    with patch.object(generic, "_dyn_client", return_value=fake_dyn):
        out = generic.apply_yaml(
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n"
            "---\n"
            "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: d\n"
        )
    assert "ConfigMap/c" in out
    assert "Deployment/d" in out


def test_apply_yaml_raises_for_unknown_kind(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()
    fake_dyn = _FakeDynClient(resources={})
    with patch.object(generic, "_dyn_client", return_value=fake_dyn):
        with pytest.raises(ValueError, match="Unknown kind"):
            generic.apply_yaml("apiVersion: v1\nkind: WeirdKind\nmetadata:\n  name: x\n")


# =============================================================================
# get_resource_yaml: managedFields stripping
# =============================================================================


def test_strip_managed_metadata_removes_server_fields_by_default():
    obj = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "p",
            "namespace": "default",
            "managedFields": [{"manager": "kuebctl", "operation": "Update"}],
            "resourceVersion": "12345",
            "uid": "abc-123",
            "generation": 1,
            "selfLink": "/api/v1/namespaces/default/pods/p",
        },
        "spec": {"containers": [{"name": "c", "image": "nginx"}]},
    }
    out = generic._strip_managed_metadata(obj, include_managed_fields=False)
    assert "managedFields" not in out["metadata"]
    assert "resourceVersion" not in out["metadata"]
    assert "uid" not in out["metadata"]
    assert "generation" not in out["metadata"]
    assert "selfLink" not in out["metadata"]
    # user-meaningful metadata kept
    assert out["metadata"]["name"] == "p"
    assert out["metadata"]["namespace"] == "default"
    # spec untouched
    assert out["spec"] == obj["spec"]


def test_strip_managed_metadata_keeps_managed_fields_when_requested():
    obj = {
        "metadata": {
            "name": "p",
            "managedFields": [{"manager": "kubectl"}],
            "resourceVersion": "99",
        }
    }
    out = generic._strip_managed_metadata(obj, include_managed_fields=True)
    # No stripping — same dict, no copy needed
    assert out is obj
    assert "managedFields" in out["metadata"]
    assert "resourceVersion" in out["metadata"]


def test_strip_managed_metadata_no_op_when_no_managed_fields_present():
    obj = {"metadata": {"name": "p", "namespace": "default"}}
    out = generic._strip_managed_metadata(obj, include_managed_fields=False)
    # Avoids a needless copy when there's nothing to strip
    assert out is obj


def test_strip_managed_metadata_handles_missing_metadata():
    obj = {"spec": {"foo": "bar"}}
    out = generic._strip_managed_metadata(obj, include_managed_fields=False)
    assert out is obj


def test_strip_managed_metadata_handles_non_dict():
    out = generic._strip_managed_metadata("not a dict", include_managed_fields=False)
    assert out == "not a dict"


# =============================================================================
# CRD support — api_version parameter + auto-discovery
# =============================================================================


def _make_fake_dc_with_crd(name="Certificate", group_version="cert-manager.io/v1"):
    """Fake DynamicClient that has a CRD registered at the given api_version.

    Mimics how DynamicClient.search() enumerates resources when api_version
    is unknown — returns a single-resource list when the kind is unique.
    """
    fake_resource = _FakeResource()

    class _CrdResources(_FakeResources):
        # Override get() so it returns the CRD on no-api-version lookup too,
        # mirroring what DynamicClient actually does.
        def get(self, api_version=None, kind=None):
            # When api_version is None (auto-discovery), match by kind only.
            if kind == name and api_version in (None, group_version):
                return fake_resource
            raise ResourceNotFoundError(f"nope {api_version}/{kind}")

        def search(self, kind=None, **kwargs):
            # Single unique match: the CRD.
            if kind == name:
                return [_FakeMatch(group_version=group_version, name=name)]
            return []

    return _FakeDynClient(resources={name: fake_resource, "__crd": _CrdResources})


class _FakeMatch:
    """Mimics a ResourceList returned by discovery.search()."""

    def __init__(self, group_version, name):
        self.group_version = group_version
        self.name = name


def test_list_resources_with_explicit_api_version_finds_crd(monkeypatch):
    """Pass api_version explicitly → uses that, no discovery needed."""
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()
    fake = _make_fake_dc_with_crd()

    # Override the _FakeResources.get to support api_version-based lookup
    fake_resource = fake._resources["Certificate"]
    fake._resources["Certificate"] = fake_resource  # already there

    # Extend the fake to handle api_version-aware get
    class _CrdAwareResources(_FakeResources):
        def get(self, api_version=None, kind=None):
            if kind == "Certificate" and api_version == "cert-manager.io/v1":
                return fake_resource
            if kind == "Certificate":
                # discovery path: search returns one match → return it
                return fake_resource
            raise ResourceNotFoundError(f"nope {api_version}/{kind}")

        def search(self, kind=None, **kwargs):
            if kind == "Certificate":
                return [_FakeMatch("cert-manager.io/v1", "Certificate")]
            return []

    class _CrdAwareDyn:
        @property
        def resources(self):
            return _CrdAwareResources({"Certificate": fake_resource})

    with patch.object(generic, "_dyn_client", return_value=_CrdAwareDyn()):
        out = generic.list_resources(
            "Certificate",
            namespace="cert-manager",
            api_version="cert-manager.io/v1",
        )
    # The fake FakeResource.get returns the resource — table row populated.
    assert isinstance(out, str)


def test_list_resources_auto_discovers_crd_kind(monkeypatch):
    """Without api_version, _resource_for_kind falls back to discovery."""
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()

    fake_resource = _FakeResource()

    class _CrdOnlyResources(_FakeResources):
        def get(self, api_version=None, kind=None):
            # Discovery path — match by kind alone.
            if kind == "Certificate":
                return fake_resource
            raise ResourceNotFoundError(f"nope {kind}")

        def search(self, kind=None, **kwargs):
            if kind == "Certificate":
                return [_FakeMatch("cert-manager.io/v1", "Certificate")]
            return []

    class _CrdOnlyDyn:
        @property
        def resources(self):
            return _CrdOnlyResources({"Certificate": fake_resource})

    with patch.object(generic, "_dyn_client", return_value=_CrdOnlyDyn()):
        # No api_version passed — must auto-resolve via discovery.
        out = generic.list_resources("Certificate", namespace="default")
    assert isinstance(out, str)


def test_resource_for_kind_raises_on_ambiguous_kind(monkeypatch):
    """If search() returns 2+ matches (same kind in multiple groups),
    _resource_for_kind must raise ValueError pointing at the options."""
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()

    class _AmbiguousResources(_FakeResources):
        def get(self, api_version=None, kind=None):
            # Both matches succeed under any api_version — triggers NotUniqueError.
            if kind == "Deployment":
                return _FakeResource()
            raise ResourceNotFoundError(f"nope {kind}")

        def search(self, kind=None, **kwargs):
            if kind == "Deployment":
                return [
                    _FakeMatch("apps/v1", "Deployment"),
                    _FakeMatch("custom.io/v1alpha1", "Deployment"),
                ]
            return []

    class _AmbiguousDyn:
        @property
        def resources(self):
            return _AmbiguousResources({})

    # Wrap raise of ResourceNotUniqueError — emulate DynamicClient.

    class _AmbResources2(_AmbiguousResources):
        def get(self, api_version=None, kind=None):
            if kind == "Deployment":
                raise ResourceNotUniqueError(
                    "Multiple matches found for {'kind': 'Deployment'}"
                )
            raise ResourceNotFoundError(f"nope {kind}")

    class _AmbDyn2:
        @property
        def resources(self):
            return _AmbResources2({})

    with patch.object(generic, "_dyn_client", return_value=_AmbDyn2()):
        with pytest.raises(ValueError, match="Ambiguous kind 'Deployment'") as ei:
            generic.get_resource("Deployment", name="x")
        # The error message must list both api_versions so the agent can pick.
        assert "apps/v1" in str(ei.value)
        assert "custom.io/v1alpha1" in str(ei.value)


def test_resource_for_kind_raises_with_clear_message_when_kind_unknown(monkeypatch):
    """Helpful error pointing the agent at get_api_resources()."""
    reset_settings_cache()

    class _EmptyResources(_FakeResources):
        def get(self, api_version=None, kind=None):
            raise ResourceNotFoundError(f"nope {kind}")

        def search(self, kind=None, **kwargs):
            return []

    class _EmptyDyn:
        @property
        def resources(self):
            return _EmptyResources({})

    with patch.object(generic, "_dyn_client", return_value=_EmptyDyn()):
        with pytest.raises(ValueError) as ei:
            generic.get_resource("NotAKind", name="x")
        msg = str(ei.value)
        assert "NotAKind" in msg
        assert "get_api_resources" in msg


def test_resource_for_kind_with_explicit_api_version_falls_through_cleanly(monkeypatch):
    """Explicit api_version: if the resource exists there, return it."""
    reset_settings_cache()
    fake_resource = _FakeResource()

    class _CrdResources(_FakeResources):
        def get(self, api_version=None, kind=None):
            if kind == "Certificate" and api_version == "cert-manager.io/v1":
                return fake_resource
            raise ResourceNotFoundError(f"nope {api_version}/{kind}")

    class _Dyn:
        @property
        def resources(self):
            return _CrdResources({"Certificate": fake_resource})

    with patch.object(generic, "_dyn_client", return_value=_Dyn()):
        obj = generic.get_resource(
            "Certificate",
            name="my-cert",
            namespace="default",
            api_version="cert-manager.io/v1",
        )
    # FakeResource.get returns _FakeApplied; _to_dict → {"metadata": {"name": "my-cert"}}
    assert obj["metadata"]["name"] == "my-cert"


def test_resource_for_kind_explicit_api_version_with_wrong_version_errors(monkeypatch):
    """When api_version is explicit and resource doesn't match there,
    error must mention the api_version (don't silently fall to discovery)."""
    reset_settings_cache()

    class _CrdResources(_FakeResources):
        def get(self, api_version=None, kind=None):
            raise ResourceNotFoundError(f"nope {api_version}/{kind}")

    class _Dyn:
        @property
        def resources(self):
            return _CrdResources({})

    with patch.object(generic, "_dyn_client", return_value=_Dyn()):
        with pytest.raises(ValueError) as ei:
            generic.get_resource(
                "Certificate",
                name="x",
                api_version="wrong.io/v1",
            )
        msg = str(ei.value)
        assert "wrong.io/v1" in msg
        # Should NOT silently try discovery when caller was explicit.
        assert "Ambiguous" not in msg


# ---- fakes --------------------------------------------------------------------


class _FakeResource:
    def apply(self, body, namespace=None):
        return _FakeApplied(body["metadata"]["name"])

    def get(self, name=None, namespace=None, **kwargs):
        from kubernetes.dynamic.exceptions import NotFoundError
        # List-style call (no `name`): return a tiny list envelope.
        if name is None:
            return _FakeResourceList()
        if name == "__missing__":
            raise NotFoundError("not found")
        return _FakeApplied(name)

    def create(self, body, namespace=None, **kwargs):
        return _FakeApplied(body["metadata"]["name"])

    def patch(self, body, namespace=None, **kwargs):
        return _FakeApplied(body["metadata"]["name"])

    def delete(self, name, namespace=None, **kwargs):
        return None


class _FakeResourceList:
    """Minimal stand-in for a list response — `.items` is empty."""

    def __init__(self):
        self.items = []


class _FakeApplied:
    def __init__(self, name):
        self._name = name

    def to_dict(self):
        return {"metadata": {"name": self._name}}


class _FakeApplied:
    def __init__(self, name):
        self._name = name

    def to_dict(self):
        return {"metadata": {"name": self._name}}


class _FakeDynClient:
    def __init__(self, resources):
        self._resources = resources

    @property
    def resources(self):
        return _FakeResources(self._resources)


class _FakeResources:
    def __init__(self, resources):
        self._resources = resources

    def get(self, api_version=None, kind=None):
        if kind not in self._resources:
            from kubernetes.dynamic.exceptions import ResourceNotFoundError
            raise ResourceNotFoundError(f"nope {kind}")
        return self._resources[kind]


# =============================================================================
# list_resources — wide mode (kubectl `-o wide` style extra columns)
# =============================================================================


class _FakeListResult:
    """Mimics the `.items` envelope on a DynamicClient list response."""

    def __init__(self, items):
        self.items = items  # plain list[dict]


class _FakeListResource:
    """A fake Resource whose `.get(name=None)` returns a predetermined list.

    Items are plain dicts so `_to_dict`'s `dict(resource)` branch handles them.

    `last_get_kwargs` captures whatever kwargs were passed to `get()`, so
    tests can assert label_selector / field_selector / limit were forwarded
    to the apiserver (not silently dropped).
    """

    def __init__(self, items):
        self._items = items
        self.last_get_kwargs: dict = {}

    def get(self, name=None, namespace=None, **kwargs):
        if name is not None:
            from kubernetes.dynamic.exceptions import NotFoundError
            raise NotFoundError("list-mode tests should not fetch by name")
        # Capture the kwargs the production code passes so we can assert
        # the limit / field_selector plumbing works.
        self.last_get_kwargs = {"namespace": namespace, **kwargs}
        return _FakeListResult(self._items)


def test_list_resources_default_keeps_four_columns(monkeypatch):
    """wide=False (default) preserves the original NAME/NAMESPACE/STATUS/AGE
    shape — backward-compatible with callers that grep for those headers."""
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()

    items = [
        {
            "metadata": {"name": "node-1"},
            "status": {"addresses": [{"type": "InternalIP", "address": "10.0.0.5"}]},
        }
    ]

    class _Dyn:
        @property
        def resources(self):
            return _FakeResources({"Node": _FakeListResource(items)})

    with patch.object(generic, "_dyn_client", return_value=_Dyn()):
        out = generic.list_resources("Node")
    # Headers
    assert "NAME" in out
    assert "NAMESPACE" in out
    assert "STATUS" in out
    assert "AGE" in out
    # Wide columns absent
    assert "INTERNAL-IP" not in out
    assert "ROLES" not in out
    assert "10.0.0.5" not in out


def test_list_resources_wide_node_adds_internal_ip_and_roles(monkeypatch):
    """wide=True on Node adds INTERNAL-IP and ROLES — the main user request:
    avoid a follow-up `get_resource_jsonpath` call just to surface the IP."""
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()

    items = [
        {
            "metadata": {
                "name": "node-1",
                "labels": {
                    "node-role.kubernetes.io/control-plane": "",
                    "node-role.kubernetes.io/worker": "",
                },
            },
            "status": {"addresses": [{"type": "InternalIP", "address": "10.0.0.5"}]},
        }
    ]

    class _Dyn:
        @property
        def resources(self):
            return _FakeResources({"Node": _FakeListResource(items)})

    with patch.object(generic, "_dyn_client", return_value=_Dyn()):
        out = generic.list_resources("Node", wide=True)

    assert "INTERNAL-IP" in out
    assert "10.0.0.5" in out
    assert "ROLES" in out
    # Both roles surface; order is stable (sorted).
    assert "control-plane" in out
    assert "worker" in out
    assert "node-1" in out


def test_list_resources_wide_service_adds_cluster_ip_and_ports(monkeypatch):
    """wide=True on Service surfaces CLUSTER-IP, PORT(S), EXTERNAL-IP — the
    three fields an agent almost always wants after listing Services."""
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()

    items = [
        {
            "metadata": {"name": "web", "namespace": "default"},
            "spec": {
                "clusterIP": "10.96.10.5",
                "ports": [
                    {"port": 80, "targetPort": 8080, "protocol": "TCP"},
                    {"port": 443, "targetPort": 8443, "protocol": "TCP"},
                ],
            },
            "status": {"loadBalancer": {"ingress": [{"ip": "1.2.3.4"}]}},
        }
    ]

    class _Dyn:
        @property
        def resources(self):
            return _FakeResources({"Service": _FakeListResource(items)})

    with patch.object(generic, "_dyn_client", return_value=_Dyn()):
        out = generic.list_resources("Service", wide=True)

    assert "CLUSTER-IP" in out
    assert "10.96.10.5" in out
    assert "PORT(S)" in out
    assert "80:8080/TCP" in out
    assert "443:8443/TCP" in out
    assert "EXTERNAL-IP" in out
    assert "1.2.3.4" in out


def test_list_resources_wide_deployment_adds_ready(monkeypatch):
    """wide=True on Deployment surfaces the ready/desired ratio in its own
    column — agents no longer have to drill into status.readyReplicas."""
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()

    items = [
        {
            "metadata": {"name": "web", "namespace": "default"},
            "spec": {"replicas": 3},
            "status": {"replicas": 3, "readyReplicas": 2},
        }
    ]

    class _Dyn:
        @property
        def resources(self):
            return _FakeResources({"Deployment": _FakeListResource(items)})

    with patch.object(generic, "_dyn_client", return_value=_Dyn()):
        out = generic.list_resources("Deployment", wide=True)

    assert "READY" in out
    assert "2/3" in out


def test_list_resources_wide_unknown_kind_does_not_crash(monkeypatch):
    """wide=True on a kind without a registered extractor list still works —
    just doesn't add any extra columns (graceful degradation)."""
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()

    items = [{"metadata": {"name": "x"}}]

    class _Dyn:
        @property
        def resources(self):
            return _FakeResources({"ConfigMap": _FakeListResource(items)})

    with patch.object(generic, "_dyn_client", return_value=_Dyn()):
        out = generic.list_resources("ConfigMap", wide=True)

    # Original columns present
    assert "NAME" in out
    assert "x" in out
    # No wide columns for ConfigMap
    assert "INTERNAL-IP" not in out
    assert "READY" not in out


# =============================================================================
# list_resources — server-side selectors (field_selector, limit) — P3
# =============================================================================


def test_list_resources_forwards_field_selector_to_apiserver(monkeypatch):
    """`field_selector="status.phase=Running"` must be passed to the
    underlying `.get()` — that's the whole point of this parameter:
    push the filter to the apiserver so we don't pay for 50k rows
    over the wire only to drop 49,500 of them client-side."""
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()

    items = [{"metadata": {"name": "web"}}]
    fake_resource = _FakeListResource(items)

    class _Dyn:
        @property
        def resources(self):
            return _FakeResources({"Pod": fake_resource})

    with patch.object(generic, "_dyn_client", return_value=_Dyn()):
        generic.list_resources(
            "Pod",
            namespace="app",
            field_selector="status.phase=Running",
        )
    assert fake_resource.last_get_kwargs.get("field_selector") == "status.phase=Running"
    assert fake_resource.last_get_kwargs.get("namespace") == "app"


def test_list_resources_forwards_limit_to_apiserver(monkeypatch):
    """`limit=10` must be passed as int to the underlying `.get()`."""
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()

    items = [{"metadata": {"name": "web"}}]
    fake_resource = _FakeListResource(items)

    class _Dyn:
        @property
        def resources(self):
            return _FakeResources({"Pod": fake_resource})

    with patch.object(generic, "_dyn_client", return_value=_Dyn()):
        generic.list_resources("Pod", namespace="app", limit=10)
    assert fake_resource.last_get_kwargs.get("limit") == 10
    # limit is an int, not a str — apiserver rejects str limits.
    assert isinstance(fake_resource.last_get_kwargs.get("limit"), int)


def test_list_resources_emits_truncation_hint_when_limit_hit(monkeypatch):
    """When the apiserver returns a full page (rows == limit), surface a
    footer hint so the operator / agent knows there's more data to
    narrow against. Without this, "I asked for 1, got 1" looks identical
    to "I asked for 1, and there's only 1"."""
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()

    items = [{"metadata": {"name": "web"}}]
    fake_resource = _FakeListResource(items)

    class _Dyn:
        @property
        def resources(self):
            return _FakeResources({"Pod": fake_resource})

    with patch.object(generic, "_dyn_client", return_value=_Dyn()):
        out = generic.list_resources("Pod", namespace="app", limit=1)
    assert "showing first 1 items" in out
    assert "field_selector=" in out  # points operator at the narrow knob


def test_list_resources_no_truncation_hint_when_under_limit(monkeypatch):
    """When rows < limit, don't emit the hint — the query was complete."""
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()

    items = [{"metadata": {"name": "web"}}]
    fake_resource = _FakeListResource(items)

    class _Dyn:
        @property
        def resources(self):
            return _FakeResources({"Pod": fake_resource})

    with patch.object(generic, "_dyn_client", return_value=_Dyn()):
        out = generic.list_resources("Pod", namespace="app", limit=10)
    assert "showing first" not in out


def test_list_resources_combines_label_and_field_selector(monkeypatch):
    """label_selector and field_selector are independent and both must
    be forwarded — together they push both filters to the apiserver."""
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()

    items = [{"metadata": {"name": "web"}}]
    fake_resource = _FakeListResource(items)

    class _Dyn:
        @property
        def resources(self):
            return _FakeResources({"Pod": fake_resource})

    with patch.object(generic, "_dyn_client", return_value=_Dyn()):
        generic.list_resources(
            "Pod",
            namespace="app",
            label_selector="app=web",
            field_selector="status.phase=Running",
            limit=50,
        )
    kw = fake_resource.last_get_kwargs
    assert kw.get("label_selector") == "app=web"
    assert kw.get("field_selector") == "status.phase=Running"
    assert kw.get("limit") == 50
