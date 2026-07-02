"""Tests for Prometheus integration: discovery, query, query_range, pod_metrics."""
from __future__ import annotations

import pytest
from kubernetes.client.rest import ApiException

from k8s_mcp.config import reset_settings_cache
from k8s_mcp.tools import prometheus


@pytest.fixture(autouse=True)
def _clear():
    reset_settings_cache()
    prometheus.reset_prometheus_discovery_cache()
    yield
    reset_settings_cache()
    prometheus.reset_prometheus_discovery_cache()


# =============================================================================
# _resolve_prometheus_url — discovery
# =============================================================================


class _FakeService:
    def __init__(self, ip="10.0.0.10", port=9090, name="http"):
        self.spec = type(
            "S", (), {
                "cluster_ip": ip,
                "ports": [type("P", (), {"name": name, "port": port})()] if name else [],
            }
        )()


def test_explicit_url_wins_over_discovery(monkeypatch):
    """If K8S_MCP_PROMETHEUS_URL is set, never call apiserver."""
    monkeypatch.setenv("K8S_MCP_PROMETHEUS_URL", "http://my-prom.example.com:9090")
    reset_settings_cache()
    calls = {"apiserver": 0}

    def fake_read(**kw):
        calls["apiserver"] += 1
        return _FakeService()

    class _Core:
        def read_namespaced_service(self, **kw):
            return fake_read(**kw)

    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: _Core())
    url = prometheus._resolve_prometheus_url(prometheus.get_settings())
    assert url == "http://my-prom.example.com:9090"
    assert calls["apiserver"] == 0  # no apiserver calls


def test_discovery_finds_monitoring_prometheus(monkeypatch):
    """First hit on the candidate list wins."""
    monkeypatch.delenv("K8S_MCP_PROMETHEUS_URL", raising=False)
    reset_settings_cache()
    attempts: list[tuple[str, str]] = []

    def fake_read(name, namespace, **kw):
        attempts.append((namespace, name))
        if (namespace, name) == ("monitoring", "kube-prometheus-stack-prometheus"):
            return _FakeService(ip="10.96.10.20", port=9090, name="http")
        raise ApiException(status=404, reason="not found")

    class _Core:
        def read_namespaced_service(self, name, namespace, **kw):
            return fake_read(name=name, namespace=namespace, **kw)

    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: _Core())
    url = prometheus._resolve_prometheus_url(prometheus.get_settings())
    assert url == "http://10.96.10.20:9090"
    # We stop at the first hit, not iterate the full candidate list
    assert ("monitoring", "kube-prometheus-stack-prometheus") in attempts


def test_discovery_falls_back_to_later_candidate(monkeypatch):
    """When earlier candidates 404, later ones are tried."""
    monkeypatch.delenv("K8S_MCP_PROMETHEUS_URL", raising=False)
    reset_settings_cache()

    def fake_read(name, namespace, **kw):
        if (namespace, name) == ("observability", "prometheus"):
            return _FakeService(ip="10.96.50.50", port=9090, name="http")
        raise ApiException(status=404, reason="not found")

    class _Core:
        def read_namespaced_service(self, name, namespace, **kw):
            return fake_read(name=name, namespace=namespace, **kw)

    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: _Core())
    url = prometheus._resolve_prometheus_url(prometheus.get_settings())
    assert url == "http://10.96.50.50:9090"


def test_discovery_not_found_returns_helpful_message(monkeypatch):
    """When no candidate exists, raise LookupError with the 'ask the user'
    message listing the candidates searched and how to override."""
    monkeypatch.delenv("K8S_MCP_PROMETHEUS_URL", raising=False)
    reset_settings_cache()

    def fake_read(name, namespace, **kw):
        raise ApiException(status=404, reason="not found")

    class _Core:
        def read_namespaced_service(self, name, namespace, **kw):
            return fake_read(name=name, namespace=namespace, **kw)

    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: _Core())
    with pytest.raises(LookupError) as exc:
        prometheus._resolve_prometheus_url(prometheus.get_settings())
    msg = str(exc.value)
    # The message must explicitly guide the agent / user
    assert "Ask the user" in msg
    assert "K8S_MCP_PROMETHEUS_URL" in msg
    assert "monitoring/kube-prometheus-stack-prometheus" in msg


def test_discovery_not_found_is_cached(monkeypatch):
    """After one miss, we don't keep hitting apiserver."""
    monkeypatch.delenv("K8S_MCP_PROMETHEUS_URL", raising=False)
    reset_settings_cache()
    calls = {"n": 0}

    def fake_read(name, namespace, **kw):
        calls["n"] += 1
        raise ApiException(status=404, reason="not found")

    class _Core:
        def read_namespaced_service(self, name, namespace, **kw):
            return fake_read(name=name, namespace=namespace, **kw)

    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: _Core())
    s = prometheus.get_settings()
    with pytest.raises(LookupError):
        prometheus._resolve_prometheus_url(s)
    with pytest.raises(LookupError):
        prometheus._resolve_prometheus_url(s)
    # Should be at most one apiserver sweep, not multiple
    assert calls["n"] <= len(prometheus._PROM_CANDIDATES)


def test_service_url_picks_http_port():
    svc = _FakeService(ip="10.0.0.5", port=9090, name="http")
    assert prometheus._service_url(svc) == "http://10.0.0.5:9090"


def test_service_url_falls_back_to_first_port_when_no_http_name():
    class _MultiPort:
        cluster_ip = "10.0.0.7"
        ports = [
            type("P", (), {"name": "grpc", "port": 9091})(),
            type("P", (), {"name": "reloader-web", "port": 9092})(),
        ]
    svc = type("S", (), {"spec": _MultiPort()})()
    # No name=="http" → falls back to ports[0] → grpc
    assert prometheus._service_url(svc) == "http://10.0.0.7:9091"


def test_service_url_no_cluster_ip_raises():
    class _Spec:
        cluster_ip = None
        ports = [type("P", (), {"name": "http", "port": 9090})()]
    svc = type("S", (), {"spec": _Spec()})()
    with pytest.raises(ValueError, match="no ClusterIP"):
        prometheus._service_url(svc)


def test_service_url_no_ports_falls_back_to_default():
    """No ports[0] → default 9090 (some Prometheus setups omit named ports)."""
    class _Spec:
        cluster_ip = "10.0.0.1"
        ports = []
    svc = type("S", (), {"spec": _Spec()})()
    assert prometheus._service_url(svc) == "http://10.0.0.1:9090"


# =============================================================================
# prometheus_query — happy paths + error paths
# =============================================================================


def _fake_settings(monkeypatch, url="http://prom.test:9090", token=None):
    monkeypatch.setenv("K8S_MCP_PROMETHEUS_URL", url)
    if token:
        monkeypatch.setenv("K8S_MCP_PROMETHEUS_BEARER_TOKEN", token)
    else:
        monkeypatch.delenv("K8S_MCP_PROMETHEUS_BEARER_TOKEN", raising=False)
    reset_settings_cache()


def test_query_vector_returns_table(monkeypatch):
    _fake_settings(monkeypatch)
    captured = {}

    def fake_get(path, params, base_url, bearer_token):
        captured["path"] = path
        captured["params"] = params
        captured["base_url"] = base_url
        captured["bearer_token"] = bearer_token
        return {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {
                        "metric": {"__name__": "up", "job": "prometheus"},
                        "value": [1700000000, "1"],
                    },
                ],
            },
        }

    monkeypatch.setattr(prometheus, "_prom_get", fake_get)
    out = prometheus.prometheus_query("up")
    assert captured["path"] == "/api/v1/query"
    assert captured["params"]["query"] == "up"
    assert "up" in out
    assert "prometheus" in out
    assert "1" in out


def test_query_with_time_param(monkeypatch):
    _fake_settings(monkeypatch)
    captured = {}

    def fake_get(path, params, base_url, bearer_token):
        captured.update(params)
        return {"status": "success", "data": {"resultType": "vector", "result": []}}

    monkeypatch.setattr(prometheus, "_prom_get", fake_get)
    prometheus.prometheus_query("up", time="2026-07-02T14:00:00Z")
    assert captured["time"] == "2026-07-02T14:00:00Z"


def test_query_bearer_token_forwarded(monkeypatch):
    _fake_settings(monkeypatch, token="abc123")
    captured = {}

    def fake_get(path, params, base_url, bearer_token):
        captured["token"] = bearer_token
        return {"status": "success", "data": {"resultType": "vector", "result": []}}

    monkeypatch.setattr(prometheus, "_prom_get", fake_get)
    prometheus.prometheus_query("up")
    assert captured["token"] == "abc123"


def test_query_empty_returns_helpful_notice(monkeypatch):
    _fake_settings(monkeypatch)

    def fake_get(path, params, base_url, bearer_token):
        return {"status": "success", "data": {"resultType": "vector", "result": []}}

    monkeypatch.setattr(prometheus, "_prom_get", fake_get)
    out = prometheus.prometheus_query('up{job="nope"}')
    assert "no data points" in out
    # Don't return empty string — many MCP clients hide it
    assert out.strip() != ""


def test_query_prometheus_error_raises_with_details(monkeypatch):
    _fake_settings(monkeypatch)

    def fake_get(path, params, base_url, bearer_token):
        return {
            "status": "error",
            "errorType": "bad_data",
            "error": "parse error at char 5: unexpected identifier",
        }

    monkeypatch.setattr(prometheus, "_prom_get", fake_get)
    with pytest.raises(ValueError, match="bad_data"):
        prometheus.prometheus_query("garbage")


def test_query_scalar_returns_inline(monkeypatch):
    _fake_settings(monkeypatch)

    def fake_get(path, params, base_url, bearer_token):
        return {
            "status": "success",
            "data": {"resultType": "scalar", "result": [1700000000, "42"]},
        }

    monkeypatch.setattr(prometheus, "_prom_get", fake_get)
    out = prometheus.prometheus_query("42")
    assert "42" in out


def test_query_propagates_prom_error_when_discovery_fails(monkeypatch):
    """If Prometheus isn't reachable, LookupError surfaces from query tool."""
    monkeypatch.delenv("K8S_MCP_PROMETHEUS_URL", raising=False)
    reset_settings_cache()

    class _Core:
        def read_namespaced_service(self, **kw):
            raise ApiException(status=404, reason="not found")

    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: _Core())
    with pytest.raises(LookupError, match="Ask the user"):
        prometheus.prometheus_query("up")


def test_query_propagates_http_error(monkeypatch):
    _fake_settings(monkeypatch)

    def fake_get(path, params, base_url, bearer_token):
        raise ValueError("Cannot reach Prometheus at http://prom.test:9090/api/v1/query: timed out")

    monkeypatch.setattr(prometheus, "_prom_get", fake_get)
    with pytest.raises(ValueError, match="Cannot reach Prometheus"):
        prometheus.prometheus_query("up")


# =============================================================================
# prometheus_query_range
# =============================================================================


def test_query_range_passes_step(monkeypatch):
    _fake_settings(monkeypatch)
    captured = {}

    def fake_get(path, params, base_url, bearer_token):
        captured.update(path=path, params=params)
        return {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {"__name__": "up", "job": "prom"},
                        "values": [
                            [1700000000, "1"],
                            [1700000030, "1"],
                            [1700000060, "1"],
                        ],
                    },
                ],
            },
        }

    monkeypatch.setattr(prometheus, "_prom_get", fake_get)
    out = prometheus.prometheus_query_range(
        "up", "2026-07-02T14:00:00Z", "2026-07-02T15:00:00Z", step="30s"
    )
    assert captured["path"] == "/api/v1/query_range"
    assert captured["params"]["step"] == "30s"
    assert captured["params"]["start"] == "2026-07-02T14:00:00Z"
    assert captured["params"]["end"] == "2026-07-02T15:00:00Z"
    # Output should mention each timestamp + value
    assert "up{job=\"prom\"}" in out
    assert "1" in out


def test_query_range_empty_returns_helpful_notice(monkeypatch):
    _fake_settings(monkeypatch)

    def fake_get(path, params, base_url, bearer_token):
        return {"status": "success", "data": {"resultType": "matrix", "result": []}}

    monkeypatch.setattr(prometheus, "_prom_get", fake_get)
    out = prometheus.prometheus_query_range(
        "up", "2026-07-02T14:00:00Z", "2026-07-02T15:00:00Z"
    )
    assert "no data points" in out
    assert "2026-07-02T14:00:00Z" in out


def test_query_range_handles_multiple_series(monkeypatch):
    _fake_settings(monkeypatch)

    def fake_get(path, params, base_url, bearer_token):
        return {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {"metric": {"__name__": "up", "job": "a"}, "values": [[1700000000, "1"]]},
                    {"metric": {"__name__": "up", "job": "b"}, "values": [[1700000000, "0"]]},
                ],
            },
        }

    monkeypatch.setattr(prometheus, "_prom_get", fake_get)
    out = prometheus.prometheus_query_range(
        "up", "2026-07-02T14:00:00Z", "2026-07-02T15:00:00Z"
    )
    assert 'job="a"' in out
    assert 'job="b"' in out
    # The two series should be in separate blocks (split by "===")
    assert out.count("===") >= 2


# =============================================================================
# pod_metrics — high-level wrapper
# =============================================================================


def test_pod_metrics_cpu(monkeypatch):
    _fake_settings(monkeypatch)
    captured = {}

    def fake_get(path, params, base_url, bearer_token):
        captured["path"] = path
        captured["params"] = params
        captured["base_url"] = base_url
        return {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {"metric": {"container": "app"}, "value": [1700000000, "0.024"]},
                    {"metric": {"container": "sidecar"}, "value": [1700000000, "0.001"]},
                ],
            },
        }

    monkeypatch.setattr(prometheus, "_prom_get", fake_get)
    out = prometheus.pod_metrics("nginx-7c5b-abc", "default", metric="cpu", range="5m")
    # PromQL should include the right labels and template
    assert "pod=~\"nginx-7c5b-abc\"" in captured["params"]["query"]
    assert 'namespace="default"' in captured["params"]["query"]
    assert 'container!="POD"' in captured["params"]["query"]
    assert "rate(container_cpu_usage_seconds_total" in captured["params"]["query"]
    assert "[5m]" in captured["params"]["query"]
    # Output is reshaped to per-container summary
    assert "Pod default/nginx-7c5b-abc" in out
    assert "container=app" in out
    assert "container=sidecar" in out
    assert "0.024" in out
    assert "cores" in out


def test_pod_metrics_memory_no_range(monkeypatch):
    """memory is instantaneous — no rate window in the PromQL."""
    _fake_settings(monkeypatch)
    captured = {}

    def fake_get(path, params, base_url, bearer_token):
        captured["params"] = params
        return {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {"metric": {"container": "app"}, "value": [1700000000, "142000000"]},
                ],
            },
        }

    monkeypatch.setattr(prometheus, "_prom_get", fake_get)
    out = prometheus.pod_metrics("p1", "ns", metric="memory", range="5m")
    assert "container_memory_working_set_bytes" in captured["params"]["query"]
    assert "[5m]" not in captured["params"]["query"]
    assert "142000000" in out
    assert "bytes" in out


def test_pod_metrics_network(monkeypatch):
    _fake_settings(monkeypatch)
    captured = {}

    def fake_get(path, params, base_url, bearer_token):
        captured["params"] = params
        return {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {"metric": {}, "value": [1700000000, "12345"]},
                ],
            },
        }

    monkeypatch.setattr(prometheus, "_prom_get", fake_get)
    prometheus.pod_metrics("p1", "ns", metric="network_rx", range="1m")
    assert "container_network_receive_bytes_total" in captured["params"]["query"]
    assert "[1m]" in captured["params"]["query"]


def test_pod_metrics_rejects_unknown_metric(monkeypatch):
    _fake_settings(monkeypatch)
    with pytest.raises(ValueError, match="not supported"):
        prometheus.pod_metrics("p1", "ns", metric="unicorns")


def test_pod_metrics_empty_data_returns_friendly_notice(monkeypatch):
    _fake_settings(monkeypatch)

    def fake_get(path, params, base_url, bearer_token):
        return {"status": "success", "data": {"resultType": "vector", "result": []}}

    monkeypatch.setattr(prometheus, "_prom_get", fake_get)
    out = prometheus.pod_metrics("p1", "ns", metric="cpu")
    assert "no Prometheus data" in out
    assert "metric='cpu'" in out


def test_pod_metrics_prometheus_error_raises(monkeypatch):
    _fake_settings(monkeypatch)

    def fake_get(path, params, base_url, bearer_token):
        return {
            "status": "error",
            "errorType": "bad_data",
            "error": "parse error",
        }

    monkeypatch.setattr(prometheus, "_prom_get", fake_get)
    with pytest.raises(ValueError, match="bad_data"):
        prometheus.pod_metrics("p1", "ns", metric="cpu")


def test_extract_label():
    assert prometheus._extract_label('up{job="prom", instance="x"}', "job") == "prom"
    assert prometheus._extract_label('up{job="prom", instance="x"}', "instance") == "x"
    assert prometheus._extract_label('up', "job") is None
    assert prometheus._extract_label('up{}', "job") is None


def test_ts_human_formats_unix():
    assert prometheus._ts_human(1700000000) == "2023-11-14T22:13:20Z"
    assert prometheus._ts_human("1700000000") == "2023-11-14T22:13:20Z"


def test_ts_human_invalid_passthrough():
    assert prometheus._ts_human("garbage") == "garbage"
    assert prometheus._ts_human(None) is None or prometheus._ts_human(None) == "None"


# =============================================================================
# register
# =============================================================================


def test_register_adds_three_tools():
    calls = []

    class _FakeMCP:
        def tool(self):
            def deco(fn):
                calls.append(fn.__name__)
                return fn
            return deco

    prometheus.register(_FakeMCP())
    assert "prometheus_query" in calls
    assert "prometheus_query_range" in calls
    assert "pod_metrics" in calls
