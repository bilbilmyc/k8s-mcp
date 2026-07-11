"""Tests for the new `get_endpoints` tool (v0.6.0).

Covers the legacy `Endpoints` path (no EndpointSlice installed) and the
EndpointSlice path. Rendering / port formatting helpers are exercised
without an apiserver.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from kubernetes.client.rest import ApiException

from k8s_mcp.tools import service


def _make_endpoint_slice(name, namespace, endpoints, ports=None):
    s = MagicMock()
    s.metadata.name = name
    s.metadata.namespace = namespace
    s.endpoints = []
    for ep in endpoints:
        e = MagicMock()
        e.addresses = ep.get("addresses", ["10.1.1.1"])
        e.node_name = ep.get("node", "")
        e.conditions.ready = ep.get("ready", True)
        ref = MagicMock()
        ref.name = ep.get("pod", "pod-x")
        e.target_ref = ref
        s.endpoints.append(e)
    s.ports = []
    for p in (ports or []):
        pp = MagicMock()
        pp.name = p.get("name")
        pp.port = p["port"]
        pp.protocol = p.get("protocol", "TCP")
        s.ports.append(pp)
    return s


def _make_legacy_endpoints(subsets):
    ep = MagicMock()
    ep.subsets = []
    for sub in subsets:
        s = MagicMock()
        s.addresses = []
        for a in sub.get("ready", []):
            am = MagicMock()
            am.ip = a["ip"]
            am.node_name = a.get("node", "")
            ref = MagicMock()
            ref.name = a.get("pod", "")
            am.target_ref = ref
            s.addresses.append(am)
        s.not_ready_addresses = []
        for a in sub.get("not_ready", []):
            am = MagicMock()
            am.ip = a["ip"]
            am.node_name = a.get("node", "")
            ref = MagicMock()
            ref.name = a.get("pod", "")
            am.target_ref = ref
            s.not_ready_addresses.append(am)
        s.ports = []
        for p in sub.get("ports", []):
            pp = MagicMock()
            pp.name = p.get("name")
            pp.port = p["port"]
            pp.protocol = p.get("protocol", "TCP")
            s.ports.append(pp)
        ep.subsets.append(s)
    return ep


def test_render_endpoint_slices_basic():
    slice_obj = _make_endpoint_slice(
        "svc-abc1", "default",
        endpoints=[{"pod": "web-1", "addresses": ["10.1.1.1"], "node": "n1"}],
        ports=[{"name": "http", "port": 80}],
    )
    out = service._render_endpoint_slices([slice_obj])
    assert "TARGET" in out and "READY" in out
    assert "web-1" in out and "n1" in out
    assert "http/80/TCP" in out


def test_render_endpoint_slices_empty_returns_hint():
    slice_obj = _make_endpoint_slice("svc-abc1", "default", endpoints=[])
    out = service._render_endpoint_slices([slice_obj])
    assert "no endpoints" in out


def test_get_endpoints_legacy_path():
    core = MagicMock()
    ep = _make_legacy_endpoints([
        {
            "ready": [{"ip": "10.1.1.1", "node": "n1", "pod": "web-1"}],
            "ports": [{"name": "http", "port": 80}],
        }
    ])
    core.read_namespaced_endpoints.return_value = ep

    # EndpointSlice discovery fails (cluster too old) → fallback to legacy.
    dc = MagicMock()
    dc.resources.get.side_effect = Exception("no slice API")
    stub_client = MagicMock()
    with patch.object(service, "get_api_client", return_value=stub_client), \
         patch.object(service.dynamic, "DynamicClient", return_value=dc), \
         patch.object(service, "_core_v1", return_value=core):
        out = service.get_endpoints("web", "default")
    assert "web-1" in out
    assert "n1" in out
    assert "http/80/TCP" in out


def test_get_endpoints_404_returns_hint():
    core = MagicMock()
    err = ApiException(status=404, reason="Not Found")
    core.read_namespaced_endpoints.side_effect = err

    dc = MagicMock()
    dc.resources.get.side_effect = Exception("no slice")
    stub_client = MagicMock()
    with patch.object(service, "get_api_client", return_value=stub_client), \
         patch.object(service.dynamic, "DynamicClient", return_value=dc), \
         patch.object(service, "_core_v1", return_value=core):
        out = service.get_endpoints("missing", "default")
    assert "no endpoints for Service default/missing" in out


def test_get_endpoints_uses_endpointslice_when_available():
    core = MagicMock()
    slice_obj = _make_endpoint_slice(
        "svc-abc1", "default",
        endpoints=[{"pod": "web-2", "addresses": ["10.1.1.2"]}],
        ports=[{"port": 9090, "protocol": "TCP"}],
    )
    slice_res = MagicMock()
    slice_res.get.return_value.items = [slice_obj]
    dc = MagicMock()
    dc.resources.get.return_value = slice_res

    # Patch `get_api_client` to return a stub so DynamicClient's __init__
    # doesn't try to talk to a real apiserver.
    stub_client = MagicMock()
    with patch.object(service, "get_api_client", return_value=stub_client), \
         patch.object(service.dynamic, "DynamicClient", return_value=dc), \
         patch.object(service, "_core_v1", return_value=core):
        out = service.get_endpoints("prom", "monitoring")
    assert "web-2" in out
    assert "10.1.1.2" in out
    # EndpointSlice path; legacy read_namespaced_endpoints NOT called
    core.read_namespaced_endpoints.assert_not_called()


def test_fmt_ports_handles_missing_port():
    p = MagicMock()
    p.name = "http"
    p.port = None
    p.protocol = "TCP"
    assert service._fmt_ports([p]) == ""
