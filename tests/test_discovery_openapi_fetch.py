"""Contract tests for Kubernetes 36 discovery and OpenAPI call shapes."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from kubernetes.dynamic.resource import ResourceList

from k8s_mcp.tools import discovery


def test_get_api_resources_uses_cached_dynamic_discovery(monkeypatch):
    deployment = SimpleNamespace(
        name="deployments",
        kind="Deployment",
        short_names=["deploy"],
        group_version="apps/v1",
        namespaced=True,
        verbs=["get", "list", "watch", "create", "delete"],
    )
    list_resource = ResourceList(object(), group="apps", api_version="v1", base_kind="Deployment")
    dynamic_client = SimpleNamespace(resources=[[list_resource, deployment]])
    monkeypatch.setattr(discovery, "get_dynamic_client", lambda: dynamic_client)

    out = discovery.get_api_resources(prefix="deploy")

    assert "deployments" in out
    assert "apps/v1" in out
    assert "Deployment" in out
    assert "DeploymentList" not in out


def test_get_api_resources_keeps_rows_collected_before_group_failure(monkeypatch):
    pod = SimpleNamespace(
        name="pods",
        kind="Pod",
        short_names=["po"],
        group_version="v1",
        namespaced=True,
        verbs=["get", "list"],
    )

    class _Resources:
        def __iter__(self):
            yield [pod]
            raise RuntimeError("aggregated API unavailable")

    monkeypatch.setattr(
        discovery,
        "get_dynamic_client",
        lambda: SimpleNamespace(resources=_Resources()),
    )
    out = discovery.get_api_resources()

    assert "pods" in out
    assert "Pod" in out


def test_fetch_openapi_prefers_v2_and_uses_current_call_api_keyword(monkeypatch):
    api = MagicMock()
    api.call_api.return_value = {
        "definitions": {"io.k8s.api.core.v1.Pod": {"type": "object"}},
    }
    monkeypatch.setattr(discovery, "get_api_client", lambda: api)

    spec = discovery._fetch_openapi_spec()

    assert spec["components"]["schemas"]["io.k8s.api.core.v1.Pod"] == {
        "type": "object",
    }
    _, kwargs = api.call_api.call_args
    assert kwargs["response_types_map"] == {200: "object"}
    assert "response_type" not in kwargs


def test_openapi_call_falls_back_to_kubernetes_29_keyword(monkeypatch):
    calls = []

    class _Api:
        def call_api(self, path, method, **kwargs):
            calls.append((path, method, kwargs))
            if "response_types_map" in kwargs:
                raise TypeError("got an unexpected keyword argument 'response_types_map'")
            return {"definitions": {"Pod": {"type": "object"}}}

    monkeypatch.setattr(discovery, "get_api_client", lambda: _Api())
    spec = discovery._fetch_openapi_spec()

    assert spec["components"]["schemas"] == {"Pod": {"type": "object"}}
    assert calls[1][2]["response_type"] == "object"


def test_fetch_openapi_merges_v3_documents_when_v2_is_unavailable(monkeypatch):
    api = MagicMock()

    def call_api(path, _method, **_kwargs):
        if path == "/openapi/v2":
            raise RuntimeError("v2 disabled")
        if path == "/openapi/v3":
            return {
                "paths": {
                    "api/v1": {"serverRelativeURL": "/openapi/v3/api/v1?hash=one"},
                    "apis/apps/v1": {
                        "serverRelativeURL": "/openapi/v3/apis/apps/v1?hash=two",
                    },
                },
            }
        if path == "/openapi/v3/api/v1":
            return {"components": {"schemas": {"Pod": {"type": "object"}}}}
        if path == "/openapi/v3/apis/apps/v1":
            return {"components": {"schemas": {"Deployment": {"type": "object"}}}}
        raise AssertionError(path)

    api.call_api.side_effect = call_api
    monkeypatch.setattr(discovery, "get_api_client", lambda: api)

    schemas = discovery._fetch_openapi_spec()["components"]["schemas"]

    assert set(schemas) == {"Pod", "Deployment"}
    document_calls = api.call_api.call_args_list[2:]
    assert document_calls[0].kwargs["query_params"] == [("hash", "one")]
    assert document_calls[1].kwargs["query_params"] == [("hash", "two")]
