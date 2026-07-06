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


def test_extract_promql_metric_name_handles_functions_and_ranges():
    """Metric extraction must skip topk/sum/rate and pick the real name."""
    assert prometheus._extract_promql_metric_name(
        'topk(5, sum by (pod, namespace) (rate(container_cpu_usage_seconds_total{container!=""}[5m])))'
    ) == "container_cpu_usage_seconds_total"
    assert prometheus._extract_promql_metric_name('up{job="nope"}') == "up"
    assert prometheus._extract_promql_metric_name(
        "sum(rate(node_cpu_seconds_total[5m]))"
    ) == "node_cpu_seconds_total"
    assert prometheus._extract_promql_metric_name("vector(0)") == ""
    assert prometheus._extract_promql_metric_name("") == ""


def test_empty_non_cadvisor_does_not_extra_probe(monkeypatch):
    """A bare `up{...}` empty must NOT trigger the cAdvisor diagnostic probe —
    that would mean a needless second API call on the common case.
    """
    _fake_settings(monkeypatch)
    calls = {"n": 0}

    def fake_get(path, params, base_url, bearer_token):
        calls["n"] += 1
        return {"status": "success", "data": {"resultType": "vector", "result": []}}

    monkeypatch.setattr(prometheus, "_prom_get", fake_get)
    out = prometheus.prometheus_query('up{job="nope"}')
    assert "no data points" in out
    assert "kubelet" not in out
    assert calls["n"] == 1  # only the original query, no diagnostic probe


def test_empty_cadvisor_query_probes_jobs_and_prompts_kubelet(monkeypatch):
    """The cAdvisor empty-result path must (a) probe actual jobs and (b)
    surface the `job="kubelet"` narrowing filter in its output.
    """
    _fake_settings(monkeypatch)
    calls: list[str] = []

    def fake_get(path, params, base_url, bearer_token):
        calls.append(params.get("query", ""))
        if "group by (job)" in calls[-1]:
            return {
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [
                        {"metric": {"job": "kubelet"}, "value": [1, "22"]},
                        {"metric": {"job": "prometheus"}, "value": [1, "10"]},
                    ],
                },
            }
        return {"status": "success", "data": {"resultType": "vector", "result": []}}

    monkeypatch.setattr(prometheus, "_prom_get", fake_get)
    out = prometheus.prometheus_query(
        'topk(5, sum by (pod, namespace) (rate(container_cpu_usage_seconds_total{container!=""}[5m])))'
    )
    assert "no data points" in out
    assert "kubelet" in out
    assert "prometheus" in out
    assert 'job="kubelet"' in out
    assert len(calls) == 2  # original + one diagnostic probe
    assert "group by (job) (container_cpu_usage_seconds_total)" in calls[1]


def test_empty_cadvisor_diagnostic_silent_when_probe_errors(monkeypatch):
    """If the diagnostic probe itself errors (e.g. Prometheus down), the
    diagnostic must NOT mask the original empty-result message.
    """
    _fake_settings(monkeypatch)

    def fake_get(path, params, base_url, bearer_token):
        if "group by (job)" in params.get("query", ""):
            raise ValueError("simulated Prometheus down")
        return {"status": "success", "data": {"resultType": "vector", "result": []}}

    monkeypatch.setattr(prometheus, "_prom_get", fake_get)
    out = prometheus.prometheus_query(
        'container_memory_working_set_bytes{job="kubelet"}'
    )
    assert "no data points" in out



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


def _register_tools():
    """Run register() against a fake MCP and return the list of tool names."""
    calls: list[str] = []

    class _FakeMCP:
        def tool(self):
            def deco(fn):
                calls.append(fn.__name__)
                return fn
            return deco

    prometheus.register(_FakeMCP())
    return calls


def test_register_adds_all_tools():
    calls = _register_tools()
    assert "prometheus_query" in calls
    assert "prometheus_query_range" in calls
    assert "pod_metrics" in calls
    assert "find_prometheus_service" in calls
    assert "expose_prometheus_as_nodeport" in calls
    # port-forward tools were intentionally removed — see git history
    # for the macOS/IPv6 rationale.


# =============================================================================
# _resolve_base — passed_url override is the "MCP + LLM collaboration" hook
# =============================================================================


def test_resolve_base_passed_url_wins_over_env(monkeypatch):
    """Agent can override via tool arg even if K8S_MCP_PROMETHEUS_URL is set."""
    monkeypatch.setenv("K8S_MCP_PROMETHEUS_URL", "http://env-prom.example.com:9090")
    reset_settings_cache()
    s = prometheus.get_settings()
    # passed_url takes priority — no apiserver call happens at all
    assert prometheus._resolve_base("http://agent-found.test:9090", s) == \
        "http://agent-found.test:9090"


def test_resolve_base_passed_url_trailing_slash_normalized(monkeypatch):
    """Common agent error: append a slash. Strip it."""
    monkeypatch.delenv("K8S_MCP_PROMETHEUS_URL", raising=False)
    reset_settings_cache()
    s = prometheus.get_settings()
    assert prometheus._resolve_base("http://x.test:9090/", s) == \
        "http://x.test:9090"


def test_resolve_base_falls_through_to_env(monkeypatch):
    """No passed_url → falls through to settings/env."""
    monkeypatch.setenv("K8S_MCP_PROMETHEUS_URL", "http://env-prom.example.com:9090")
    reset_settings_cache()
    s = prometheus.get_settings()
    assert prometheus._resolve_base(None, s) == "http://env-prom.example.com:9090"


def test_query_with_passed_url_skips_discovery(monkeypatch):
    """End-to-end: passing prometheus_url to the tool skips env + discovery."""
    captured = {}

    def fake_get(path, params, base_url, bearer_token):
        captured["base_url"] = base_url
        return {"status": "success", "data": {"resultType": "vector", "result": []}}

    monkeypatch.setattr(prometheus, "_prom_get", fake_get)
    # Deliberately set no env var — only the passed URL matters
    monkeypatch.delenv("K8S_MCP_PROMETHEUS_URL", raising=False)
    reset_settings_cache()
    apiserver_calls = {"n": 0}

    class _Core:
        def read_namespaced_service(self, **kw):
            apiserver_calls["n"] += 1
            return _FakeService()

    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: _Core())
    prometheus.prometheus_query("up", prometheus_url="http://agent-found.example.com:9090")
    assert captured["base_url"] == "http://agent-found.example.com:9090"
    # Discovery did NOT run — apiserver not called
    assert apiserver_calls["n"] == 0


def test_pod_metrics_with_passed_url(monkeypatch):
    """pod_metrics also honors the override."""
    captured = {}

    def fake_get(path, params, base_url, bearer_token):
        captured["base_url"] = base_url
        return {"status": "success", "data": {"resultType": "vector", "result": [
            {"metric": {"container": "app"}, "value": [1700000000, "0.01"]},
        ]}}

    monkeypatch.setattr(prometheus, "_prom_get", fake_get)
    prometheus.pod_metrics(
        "p1", "ns", metric="cpu",
        prometheus_url="http://agent-found.example.com:9090",
    )
    assert captured["base_url"] == "http://agent-found.example.com:9090"


# =============================================================================
# find_prometheus_service — broader discovery across namespaces
# =============================================================================


def _ns_service(ns, name, ip="10.0.0.1", port=9090):
    """Build a fake CoreV1Service for the discovery tests."""
    return type(
        "Svc", (), {
            "metadata": type("M", (), {"namespace": ns, "name": name})(),
            "spec": type(
                "S", (), {
                    "cluster_ip": ip,
                    "ports": [type("P", (), {"name": "http", "port": port})()],
                }
            )(),
        }
    )()


def _ns_obj(name):
    return type("Ns", (), {"metadata": type("M", (), {"name": name})()})()


def test_find_prometheus_service_scans_all_namespaces(monkeypatch):
    """When no namespace is given, lists every ns and surfaces any Service
    whose name looks like Prometheus — even in non-standard namespaces."""
    monkeypatch.delenv("K8S_MCP_PROMETHEUS_URL", raising=False)
    reset_settings_cache()

    all_namespaces = [_ns_obj("default"), _ns_obj("monitoring")]
    services_by_ns = {
        "default": [_ns_service("default", "monitor-kube-prometheus-st-prometheus", ip="10.96.10.5", port=9090)],
        "monitoring": [_ns_service("monitoring", "kube-prometheus-stack-prometheus", ip="10.96.20.5", port=9090)],
    }

    class _Core:
        def list_namespace(self, **kw):
            return type("R", (), {"items": all_namespaces})()

        def list_namespaced_service(self, namespace, **kw):
            return type("R", (), {"items": services_by_ns.get(namespace, [])})()

        def list_service_for_all_namespaces(self, **kw):
            flat = []
            for items in services_by_ns.values():
                flat.extend(items)
            return type("R", (), {"items": flat})()

    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: _Core())
    out = prometheus.find_prometheus_service()
    # Both non-standard and standard names should appear (table format)
    assert "monitor-kube-prometheus-st-prometheus" in out
    assert "kube-prometheus-stack-prometheus" in out
    # And the URLs the agent should pass back
    assert "http://10.96.10.5:9090" in out
    assert "http://10.96.20.5:9090" in out
    # ns separation visible
    assert "default" in out
    assert "monitoring" in out


def test_find_prometheus_service_namespace_filter(monkeypatch):
    """When namespace is given, only scans that one ns."""
    monkeypatch.delenv("K8S_MCP_PROMETHEUS_URL", raising=False)
    reset_settings_cache()

    services = [_ns_service("default", "prometheus", ip="10.96.99.1", port=9090)]

    class _Core:
        def list_namespace(self, **kw):
            return type("R", (), {"items": []})()

        def list_namespaced_service(self, namespace, **kw):
            assert namespace == "default"
            return type("R", (), {"items": services})()

    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: _Core())
    out = prometheus.find_prometheus_service(namespace="default")
    assert "default" in out
    assert "prometheus" in out
    assert "http://10.96.99.1:9090" in out


def test_find_prometheus_service_filters_non_matching_names(monkeypatch):
    """Services whose names don't include 'prometheus' / 'prom' are skipped."""
    monkeypatch.delenv("K8S_MCP_PROMETHEUS_URL", raising=False)
    reset_settings_cache()

    services = [
        _ns_service("default", "my-app"),
        _ns_service("default", "prometheus-operated"),
    ]

    class _Core:
        def list_namespace(self, **kw):
            return type("R", (), {"items": [_ns_obj("default")]})()

        def list_namespaced_service(self, namespace, **kw):
            return type("R", (), {"items": services})()

        def list_service_for_all_namespaces(self, **kw):
            return type("R", (), {"items": services})()

    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: _Core())
    out = prometheus.find_prometheus_service()
    assert "my-app" not in out
    assert "prometheus-operated" in out


def test_find_prometheus_service_empty_returns_helpful_notice(monkeypatch):
    """When nothing is found, return a 'no results' notice (not an empty string)."""
    monkeypatch.delenv("K8S_MCP_PROMETHEUS_URL", raising=False)
    reset_settings_cache()

    class _Core:
        def list_namespace(self, **kw):
            return type("R", (), {"items": [_ns_obj("default")]})()

        def list_namespaced_service(self, namespace, **kw):
            return type("R", (), {"items": [_ns_service("default", "nginx")]})()

    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: _Core())
    out = prometheus.find_prometheus_service()
    assert "No Prometheus-looking Services" in out
    assert out.strip() != ""
    # Should hint at the env-var workaround
    assert "K8S_MCP_PROMETHEUS_URL" in out


# Helpers for the RECOMMENDED-column tests: a fake Service with an
# explicit `type` field (the default helper above leaves it unset).


def _ns_service_with_type(ns, name, ip="10.0.0.1", port=9090, svc_type=None, node_port=None):
    """Same as `_ns_service` but with `spec.type` populated and optional `node_port`."""
    return type(
        "Svc", (), {
            "metadata": type("M", (), {"namespace": ns, "name": name})(),
            "spec": type(
                "S", (), {
                    "cluster_ip": ip,
                    "type": svc_type,
                    "ports": [
                        type("P", (), {"name": "http", "port": port, "node_port": node_port})()
                    ],
                }
            )(),
        }
    )()


def test_find_prometheus_service_clusterip_row_recommends_nodeport(monkeypatch):
    """ClusterIP rows must call out `expose_prometheus_as_nodeport` literally,
    so the agent sees the exact next-call signature in the table."""
    monkeypatch.delenv("K8S_MCP_PROMETHEUS_URL", raising=False)
    reset_settings_cache()

    class _Core:
        def list_namespace(self, **kw):
            return type("R", (), {"items": [_ns_obj("monitoring")]})()

        def list_namespaced_service(self, namespace, **kw):
            return type("R", (), {
                "items": [
                    _ns_service_with_type(
                        "monitoring",
                        "kube-prometheus-stack-prometheus",
                        ip="10.96.42.7",
                        svc_type="ClusterIP",
                    ),
                ],
            })()

        def list_service_for_all_namespaces(self, **kw):
            return type("R", (), {
                "items": [
                    _ns_service_with_type(
                        "monitoring",
                        "kube-prometheus-stack-prometheus",
                        ip="10.96.42.7",
                        svc_type="ClusterIP",
                    ),
                ],
            })()

    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: _Core())
    out = prometheus.find_prometheus_service()

    # Table column headers must include the new TYPE/RECOMMENDED columns.
    assert "TYPE" in out
    assert "RECOMMENDED" in out
    assert "ClusterIP" in out

    # The exact call signature must appear (so an LLM can copy it verbatim).
    assert "expose_prometheus_as_nodeport(" in out
    assert "namespace='monitoring'" in out
    assert "service_name='kube-prometheus-stack-prometheus'" in out

    # ClusterIP URL must be annotated as not-reachable to discourage
    # the agent from plugging 10.96.x.x straight into prometheus_query.
    assert "http://10.96.42.7:9090" in out
    assert "NOT reachable" in out

    # Guidance block — must promote the NodePort tool (the port-forward
    # warning lived here before; it's now in NodePort's docstring).
    assert "RECOMMENDED" in out
    assert "expose_prometheus_as_nodeport" in out


def test_find_prometheus_service_nodeport_row_says_direct(monkeypatch):
    """Already-NodePort Services must show ✅ direct so the agent skips
    both bridge tools and goes straight to list_resources(kind=Node)."""
    monkeypatch.delenv("K8S_MCP_PROMETHEUS_URL", raising=False)
    reset_settings_cache()

    class _Core:
        def list_namespace(self, **kw):
            return type("R", (), {"items": [_ns_obj("monitoring")]})()

        def list_namespaced_service(self, namespace, **kw):
            return type("R", (), {
                "items": [
                    _ns_service_with_type(
                        "monitoring",
                        "kube-prometheus-stack-prometheus",
                        port=9090,
                        svc_type="NodePort",
                        node_port=45149,
                    ),
                ],
            })()

        def list_service_for_all_namespaces(self, **kw):
            return type("R", (), {
                "items": [
                    _ns_service_with_type(
                        "monitoring",
                        "kube-prometheus-stack-prometheus",
                        port=9090,
                        svc_type="NodePort",
                        node_port=45149,
                    ),
                ],
            })()

    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: _Core())
    out = prometheus.find_prometheus_service()

    assert "NodePort" in out
    assert "✅ direct" in out
    # The literal call signature with namespace/name should NOT appear in the
    # table cell — that branch is only for ClusterIP rows. The tool *name*
    # may still appear in the static guidance text.
    assert "namespace='monitoring'" not in out
    assert "service_name='kube-prometheus-stack-prometheus'" not in out
    # Bug fix: NodePort URL must use the apiserver-allocated nodePort
    # (45149), NOT the clusterIP port (9090). Pre-fix code wrote
    # `http://<node-ip>:{port}` which was always 9090 for a default
    # kube-prometheus-stack Service.
    assert "http://<node-ip>:45149" in out
    assert "http://<node-ip>:9090" not in out
    # And the new NODE_PORT column surfaces the same number.
    assert "NODE_PORT" in out
    assert "45149" in out
    # And guidance that mentions port-forward caveats should NOT appear,
    # since no ClusterIP rows are present.
    # (Guidance still appears for NodePort/LoadBalancer rows with a different
    #  branch — but the macOS warning specifically is gated on ClusterIP.)


def test_find_prometheus_service_loadbalancer_row_says_direct(monkeypatch):
    """LoadBalancer rows also get ✅ direct (substitute LB ingress)."""
    monkeypatch.delenv("K8S_MCP_PROMETHEUS_URL", raising=False)
    reset_settings_cache()

    class _Core:
        def list_namespace(self, **kw):
            return type("R", (), {"items": [_ns_obj("monitoring")]})()

        def list_namespaced_service(self, namespace, **kw):
            return type("R", (), {
                "items": [
                    _ns_service_with_type(
                        "monitoring",
                        "kube-prometheus-stack-prometheus",
                        port=9090,
                        svc_type="LoadBalancer",
                        node_port=30080,
                    ),
                ],
            })()

        def list_service_for_all_namespaces(self, **kw):
            return type("R", (), {
                "items": [
                    _ns_service_with_type(
                        "monitoring",
                        "kube-prometheus-stack-prometheus",
                        port=9090,
                        svc_type="LoadBalancer",
                        node_port=30080,
                    ),
                ],
            })()

    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: _Core())
    out = prometheus.find_prometheus_service()

    assert "LoadBalancer" in out
    assert "✅ direct" in out
    assert "LB ingress" in out or "Service.status" in out
    # LoadBalancer URL uses service port (the LB forwards to that port),
    # NOT nodePort — LoadBalancer doesn't have one and using 30080 here
    # would mislead the agent.
    assert "http://<lb-ip>:9090" in out
    assert "http://<lb-ip>:30080" not in out
    # NODE_PORT column stays empty for LoadBalancer.
    # We can't easily assert "empty cell" against short_table's padded output,
    # but we can assert the header is there (proving wide format is active).
    assert "NODE_PORT" in out


def test_find_prometheus_service_guidance_omits_portforward_warning_when_only_nodeport(monkeypatch):
    """The macOS port-forward caveat is gated on ClusterIP rows being
    present. With only NodePort rows, we omit it to avoid FUD about a
    path the agent wouldn't need anyway."""
    monkeypatch.delenv("K8S_MCP_PROMETHEUS_URL", raising=False)
    reset_settings_cache()

    class _Core:
        def list_namespace(self, **kw):
            return type("R", (), {"items": [_ns_obj("monitoring")]})()

        def list_namespaced_service(self, namespace, **kw):
            return type("R", (), {
                "items": [
                    _ns_service_with_type(
                        "monitoring",
                        "kube-prometheus-stack-prometheus",
                        svc_type="NodePort",
                    ),
                ],
            })()

        def list_service_for_all_namespaces(self, **kw):
            return type("R", (), {
                "items": [
                    _ns_service_with_type(
                        "monitoring",
                        "kube-prometheus-stack-prometheus",
                        svc_type="NodePort",
                    ),
                ],
            })()

    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: _Core())
    out = prometheus.find_prometheus_service()

    # Guidance is still present (top-level NodePort / LoadBalancer section)
    assert "Guidance" in out
    # But the port-forward macOS warning is not, since no ClusterIP row.
    assert "macOS" not in out
    assert "[Errno 61]" not in out



# expose_prometheus_as_nodeport — ClusterIP → NodePort clone
# =============================================================================


def _make_service(ns, name, *, type_="ClusterIP", ip="10.0.0.10",
                  selector=None, ports=None, node_port=None, labels=None):
    """A richer fake CoreV1Service for the NodePort tests.

    Defaults to a representative ClusterIP-with-selector that's
    clonable. Pass `node_port=` for an already-externally-reachable one.
    """
    if ports is None:
        ports = [{"name": "http", "port": 9090, "target_port": 9090,
                  "node_port": node_port}]
    port_objs = [
        type("P", (), {
            "name": p["name"], "port": p["port"],
            "target_port": p.get("target_port"),
            "protocol": p.get("protocol"),
            "node_port": p.get("node_port"),
        })()
        for p in ports
    ]
    return type(
        "Svc", (), {
            "metadata": type("M", (), {
                "namespace": ns, "name": name, "labels": labels or {},
            })(),
            "spec": type("S", (), {
                "type": type_, "cluster_ip": ip,
                "selector": selector or {},
                "ports": port_objs,
            })(),
        }
    )()


class _FakeCoreForNodeport:
    """Stands in for CoreV1Api with the calls expose_prometheus_as_nodeport makes."""

    def __init__(self, services_by_ns, namespaces=None, on_create=None):
        self._services_by_ns = services_by_ns
        self._namespaces = namespaces or list(services_by_ns.keys())
        self._on_create = on_create

    def list_namespace(self):
        return type("R", (), {
            "items": [type("Ns", (), {"metadata": type("M", (), {"name": n})()})()
                      for n in self._namespaces],
        })()

    def list_namespaced_service(self, namespace):
        return type("R", (), {
            "items": list(self._services_by_ns.get(namespace, [])),
        })()

    def read_namespaced_service(self, name, namespace):
        for svc in self._services_by_ns.get(namespace, []):
            if svc.metadata.name == name:
                return svc
        # Mirror K8s 404
        err = ApiException(status=404, reason="Not Found")
        raise err

    def create_namespaced_service(self, namespace, body):
        if self._on_create:
            return self._on_create(namespace, body)
        # Add to fake registry and return as-is
        self._services_by_ns.setdefault(namespace, []).append(body)
        return body


def test_nodeport_already_nodeport_short_circuits(monkeypatch):
    """If the source is already NodePort, return its URL — don't clone."""
    fake = _FakeCoreForNodeport({
        "monitoring": [_make_service(
            "monitoring", "prometheus",
            type_="NodePort", selector={"app": "prom"},
            node_port=31234,
        )],
    })
    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: fake)
    out = prometheus.expose_prometheus_as_nodeport("monitoring", "prometheus")
    assert "already type=NodePort" in out
    assert "no new Service was created" in out
    # No second Service was created
    assert len(fake._services_by_ns["monitoring"]) == 1


def test_nodeport_loadbalancer_also_short_circuits(monkeypatch):
    fake = _FakeCoreForNodeport({
        "monitoring": [_make_service(
            "monitoring", "prometheus", type_="LoadBalancer",
            selector={"app": "prom"},
        )],
    })
    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: fake)
    out = prometheus.expose_prometheus_as_nodeport("monitoring", "prometheus")
    assert "LoadBalancer" in out
    assert "no new Service was created" in out


def test_nodeport_clusterip_creates_clone(monkeypatch):
    """ClusterIP → a new NodePort clone is created; apiserver picks nodePort.

    We stub the apiserver's auto-allocation by making `create_namespaced_service`
    fill in nodePort=31402 on the way back — that's exactly what the real
    apiserver would do, just deterministic for the test.
    """
    fake = _FakeCoreForNodeport({
        "monitoring": [_make_service(
            "monitoring", "prometheus",
            type_="ClusterIP", ip="10.96.10.20",
            selector={"app": "prom"},
            ports=[{"name": "http", "port": 9090, "target_port": 9090}],
        )],
    }, namespaces=["monitoring"])

    def simulate_apiserver_allocate(namespace, body):
        # Pretend the apiserver picked a free port and stamped it onto the
        # returned object. Our code is responsible for *not* having set one.
        body.spec.ports[0].node_port = 31402
        fake._services_by_ns.setdefault(namespace, []).append(body)
        return body

    fake._on_create = simulate_apiserver_allocate
    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: fake)
    out = prometheus.expose_prometheus_as_nodeport("monitoring", "prometheus")
    assert "Created NodePort clone" in out
    assert "monitoring/prometheus-np" in out
    assert "31402" in out
    created = fake._services_by_ns["monitoring"][-1]
    assert created.spec.type == "NodePort"
    assert created.spec.selector == {"app": "prom"}


def test_nodeport_does_not_set_node_port_in_request(monkeypatch):
    """The body sent to the apiserver must NOT carry a node_port.

    This is the whole point of the apiserver-auto-allocate fix: passing
    a numeric node_port creates a TOCTOU race. The apiserver is the only
    piece that holds the in-use set and can allocate atomically.
    """
    captured: dict = {}

    class _Core:
        def read_namespaced_service(self, name, namespace):
            return _make_service(
                "monitoring", "prometheus",
                type_="ClusterIP", selector={"app": "prom"},
                ports=[{"name": "http", "port": 9090, "target_port": 9090}],
            )

        def create_namespaced_service(self, namespace, body):
            # Snapshot what's actually being sent — before the apiserver
            # layer of the test fixture fills in an allocated port.
            captured["sent_node_port"] = body.spec.ports[0].node_port
            body.spec.ports[0].node_port = 31200
            return body

    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: _Core())
    prometheus.expose_prometheus_as_nodeport("monitoring", "prometheus")
    # The body we hand to the apiserver carries no node_port — the
    # apiserver fills it in atomically (single-leader, in-memory set).
    assert captured["sent_node_port"] is None, (
        "Tool sent an explicit node_port; this is the TOCTOU race "
        "we deliberately avoid."
    )


def test_nodeport_targets_http_port_only(monkeypatch):
    """Multi-port Service (http + reloader-web): only http is cloned.

    kube-prometheus-stack adds ports like `reloader-web` to the same
    Service; cloning all of them wastes NodePort slots and confuses the
    caller with multiple URLs.
    """
    fake = _FakeCoreForNodeport({
        "monitoring": [_make_service(
            "monitoring", "prometheus",
            type_="ClusterIP", ip="10.96.10.20",
            selector={"app": "prom"},
            ports=[
                {"name": "reloader-web", "port": 9090, "target_port": 9090},
                {"name": "http", "port": 9090, "target_port": 9090},
            ],
        )],
    }, namespaces=["monitoring"])

    captured = {}

    def grab_body(namespace, body):
        captured["body"] = body
        body.spec.ports[0].node_port = 31200
        return body

    fake._on_create = grab_body
    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: fake)
    prometheus.expose_prometheus_as_nodeport("monitoring", "prometheus")
    sent_ports = captured["body"].spec.ports
    # Only one port; it's the `http` one
    assert len(sent_ports) == 1
    assert sent_ports[0].name == "http"


def test_nodeport_targets_web_when_no_http(monkeypatch):
    """If the user's Service names its port `web`, we still pick it up."""
    fake = _FakeCoreForNodeport({
        "monitoring": [_make_service(
            "monitoring", "prometheus",
            type_="ClusterIP", selector={"app": "prom"},
            ports=[{"name": "web", "port": 9090, "target_port": 9090}],
        )],
    }, namespaces=["monitoring"])

    captured = {}

    def grab_body(namespace, body):
        captured["body"] = body
        body.spec.ports[0].node_port = 31200
        return body

    fake._on_create = grab_body
    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: fake)
    prometheus.expose_prometheus_as_nodeport("monitoring", "prometheus")
    assert captured["body"].spec.ports[0].name == "web"


def test_nodeport_targets_first_port_for_unusual_names(monkeypatch):
    """If no port matches the well-known names, fall back to ports[0]."""
    fake = _FakeCoreForNodeport({
        "monitoring": [_make_service(
            "monitoring", "prometheus",
            type_="ClusterIP", selector={"app": "prom"},
            ports=[{"name": "my-custom-port", "port": 9090, "target_port": 9090}],
        )],
    }, namespaces=["monitoring"])

    captured = {}

    def grab_body(namespace, body):
        captured["body"] = body
        body.spec.ports[0].node_port = 31200
        return body

    fake._on_create = grab_body
    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: fake)
    prometheus.expose_prometheus_as_nodeport("monitoring", "prometheus")
    assert captured["body"].spec.ports[0].name == "my-custom-port"


def test_nodeport_apiserver_error_wrapped(monkeypatch):
    """If the apiserver rejects the create (e.g. quota, RBAC, 422),
    surface a helpful RuntimeError — not a raw 422."""
    class _Core:
        def read_namespaced_service(self, name, namespace):
            return _make_service(
                "monitoring", "prometheus",
                type_="ClusterIP", selector={"app": "prom"},
                ports=[{"name": "http", "port": 9090, "target_port": 9090}],
            )

        def create_namespaced_service(self, namespace, body):
            err = ApiException(status=422, reason="Unprocessable Entity")
            err.body = b'{"message":"forbidden"}'
            raise err

    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: _Core())
    with pytest.raises(RuntimeError, match="HTTP 422"):
        prometheus.expose_prometheus_as_nodeport("monitoring", "prometheus")


def test_nodeport_idempotent_reuses_existing_clone(monkeypatch):
    """If a previous call already created the NodePort clone, reuse it."""
    fake = _FakeCoreForNodeport({
        "monitoring": [
            _make_service("monitoring", "prometheus",
                          type_="ClusterIP", ip="10.96.10.20",
                          selector={"app": "prom"}),
            # Pre-existing clone from a prior call
            _make_service("monitoring", "prometheus-np",
                          type_="NodePort", selector={"app": "prom"},
                          node_port=31299),
        ],
    }, namespaces=["monitoring"])
    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: fake)
    out = prometheus.expose_prometheus_as_nodeport("monitoring", "prometheus")
    assert "already exists" in out
    # No third Service was added
    assert len(fake._services_by_ns["monitoring"]) == 2


def test_nodeport_rejects_headless_service(monkeypatch):
    """Headless services have no ClusterIP — can't be cloned."""
    fake = _FakeCoreForNodeport({
        "monitoring": [_make_service(
            "monitoring", "prometheus", type_="ClusterIP", ip=None,
            selector={"app": "prom"},
        )],
    }, namespaces=["monitoring"])
    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: fake)
    with pytest.raises(ValueError, match="Headless"):
        prometheus.expose_prometheus_as_nodeport("monitoring", "prometheus")


def test_nodeport_rejects_empty_selector(monkeypatch):
    """A Service with no selector has nothing to forward to."""
    fake = _FakeCoreForNodeport({
        "monitoring": [_make_service(
            "monitoring", "prometheus",
            type_="ClusterIP", ip="10.96.10.20", selector={},
        )],
    }, namespaces=["monitoring"])
    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: fake)
    with pytest.raises(ValueError, match="no selector"):
        prometheus.expose_prometheus_as_nodeport("monitoring", "prometheus")


def test_nodeport_rejects_empty_ports(monkeypatch):
    fake = _FakeCoreForNodeport({
        "monitoring": [_make_service(
            "monitoring", "prometheus", type_="ClusterIP",
            selector={"app": "prom"}, ports=[],
        )],
    }, namespaces=["monitoring"])
    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: fake)
    with pytest.raises(ValueError, match="no ports"):
        prometheus.expose_prometheus_as_nodeport("monitoring", "prometheus")


def test_nodeport_404_raises_value_error(monkeypatch):
    """A missing source Service → actionable error, not raw ApiException."""
    fake = _FakeCoreForNodeport({}, namespaces=[])
    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: fake)
    with pytest.raises(ValueError, match="not found"):
        prometheus.expose_prometheus_as_nodeport("monitoring", "prometheus")


def test_nodeport_read_only_raises(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "true")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="read-only"):
        prometheus.expose_prometheus_as_nodeport("monitoring", "prometheus")


def test_nodeport_namespace_not_in_allowlist(monkeypatch):
    monkeypatch.setenv("K8S_MCP_NAMESPACE_ALLOWLIST", "default,app")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="ALLOWLIST"):
        prometheus.expose_prometheus_as_nodeport("monitoring", "prometheus")


def test_nodeport_validates_inputs(monkeypatch):
    """Bad namespace / service_name / suffix → ValueError."""
    with pytest.raises(ValueError, match="invalid namespace"):
        prometheus.expose_prometheus_as_nodeport("bad ns", "prom")
    with pytest.raises(ValueError, match="invalid service_name"):
        prometheus.expose_prometheus_as_nodeport("monitoring", "BAD NAME")
    with pytest.raises(ValueError, match="must not be empty"):
        prometheus.expose_prometheus_as_nodeport("monitoring", "prom", name_suffix="")


def test_nodeport_no_retry_loop(monkeypatch):
    """Sanity: the tool submits the create ONCE. No retry — apiserver owns atomicity."""
    calls = {"n": 0}

    def counting_create(namespace, body):
        calls["n"] += 1
        body.spec.ports[0].node_port = 31402
        return body

    fake = _FakeCoreForNodeport({
        "monitoring": [_make_service(
            "monitoring", "prometheus",
            type_="ClusterIP", ip="10.96.10.20", selector={"app": "prom"},
            ports=[{"name": "http", "port": 9090, "target_port": 9090}],
        )],
    }, namespaces=["monitoring"])
    fake._on_create = counting_create
    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: fake)
    prometheus.expose_prometheus_as_nodeport("monitoring", "prometheus")
    assert calls["n"] == 1  # exactly one apiserver roundtrip


def test_nodeport_no_random_module_used(monkeypatch):
    """Defence-in-depth: no `random.randint` calls. If someone re-introduces
    client-side port picking, the test fails fast."""
    import random as _real_random
    used = {"calls": 0}
    real_randint = _real_random.randint
    def noisy_randint(a, b):
        used["calls"] += 1
        return real_randint(a, b)
    monkeypatch.setattr(_real_random, "randint", noisy_randint)
    # Probe: make sure `prometheus.random` exists (it shouldn't anymore)
    # If it does, the test below will catch any port-picking logic.
    has_random = getattr(prometheus, "random", None) is not None
    assert has_random is False, (
        "prometheus module still imports 'random' — re-introduced "
        "client-side port picking, which is racy."
    )


# =============================================================================
# _resolve_prometheus_url — wide-scan fallback (K8S_MCP_PROMETHEUS_NAMESPACE_ALLOWLIST)
# =============================================================================
#
# When the hardcoded `_PROM_CANDIDATES` list misses (e.g. Prometheus was
# deployed into `default/` by a non-standard installer), `_resolve_prometheus_url`
# falls through to a cluster-wide scan filtered by `_PROM_NAME_HINTS`. The
# fallback honors `prometheus_namespace_allowlist` so a busy cluster can
# bound the surface.


class _SvcList:
    """Minimal V1ServiceList substitute (the helper iterates `.items`)."""

    def __init__(self, items):
        self.items = items


def _svc(ns, name, ip="10.96.0.1", port=9090, svc_type=None, node_port=None):
    """Build a fake V1Service for wide-scan tests."""
    ports = [type("P", (), {"name": "http", "port": port, "node_port": node_port})()]
    return type(
        "Svc", (), {
            "metadata": type("M", (), {"namespace": ns, "name": name})(),
            "spec": type(
                "S", (), {
                    "cluster_ip": ip,
                    "type": svc_type,
                    "ports": ports,
                }
            )(),
        }
    )()


class _WideScanCore:
    """A fake CoreV1Api that backs the wide-scan code path.

    Differentiates between:
      - `read_namespaced_service(name, namespace)` → used by the hardcoded
        candidate list (always 404 here, forcing the fallback)
      - `list_service_for_all_namespaces()` → used by the production
        wide-scan path since the P1 refactor (was `list_namespace()` +
        N×`list_namespaced_service(namespace)` before)
      - `list_namespaced_service(namespace)` → still used by the
        explicit-namespace branch of `find_prometheus_service`
    """

    def __init__(self, namespaces, services_by_ns):
        self._namespaces = [_ns_obj(n) for n in namespaces]
        self._services_by_ns = services_by_ns

    def read_namespaced_service(self, name, namespace, **kw):
        # Hardcoded-candidate probe: every (ns, svc) miss → fallback fires.
        raise ApiException(status=404, reason="not found")

    def list_namespace(self, **kw):
        return type("R", (), {"items": self._namespaces})()

    def list_namespaced_service(self, namespace, **kw):
        return _SvcList(self._services_by_ns.get(namespace, []))

    def list_service_for_all_namespaces(self, **kw):
        # Flatten every namespace's services into one list — this is what
        # the P1 refactor calls. Each item already carries its
        # `metadata.namespace` so the production code filters by that.
        flat = []
        for ns_items in self._services_by_ns.values():
            flat.extend(ns_items)
        return _SvcList(flat)


def test_resolve_wide_scan_finds_non_standard_namespace(monkeypatch):
    """Hardcoded candidates miss; wide scan finds Prometheus in `default/`.
    Regression for the case where kube-prometheus-stack was deployed into
    a non-standard namespace — `_PROM_CANDIDATES` doesn't include
    `default/monitor-kube-prometheus-st-prometheus`, so discovery must
    fall through to the wide scan to find it."""
    monkeypatch.delenv("K8S_MCP_PROMETHEUS_URL", raising=False)
    monkeypatch.delenv("K8S_MCP_PROMETHEUS_NAMESPACE_ALLOWLIST", raising=False)
    reset_settings_cache()

    core = _WideScanCore(
        namespaces=["default", "kube-system"],
        services_by_ns={
            "default": [
                _svc("default", "monitor-kube-prometheus-st-prometheus",
                     ip="10.96.3.39", port=9090),
            ],
            "kube-system": [_svc("kube-system", "kube-dns")],
        },
    )
    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: core)
    url = prometheus._resolve_prometheus_url(prometheus.get_settings())
    assert url == "http://10.96.3.39:9090"


def test_resolve_wide_scan_prefers_nodeport_over_clusterip(monkeypatch):
    """When both ClusterIP and NodePort Prometheus Services exist, the
    wide scan picks NodePort first — and now returns the real externally-
    reachable URL by looking up a Node InternalIP, not the unreachable
    ClusterIP `10.96.x.x`."""
    monkeypatch.delenv("K8S_MCP_PROMETHEUS_URL", raising=False)
    monkeypatch.delenv("K8S_MCP_PROMETHEUS_NAMESPACE_ALLOWLIST", raising=False)
    reset_settings_cache()

    core = _WideScanCore(
        namespaces=["default", "monitoring"],
        services_by_ns={
            "default": [
                _svc("default", "monitor-kube-prometheus-st-prometheus",
                     ip="10.96.3.39", port=9090, svc_type="ClusterIP"),
            ],
            "monitoring": [
                _svc("monitoring", "kube-prometheus-stack-prometheus",
                     ip="10.96.20.5", port=9090, svc_type="NodePort",
                     node_port=45149),
            ],
        },
    )
    core.list_node = lambda: type("R", (), {
        "items": [
            type("N", (), {
                "status": type("S", (), {
                    "addresses": [
                        type("A", (), {"type": "InternalIP", "address": "12.2.40.40"})(),
                        type("A", (), {"type": "Hostname", "address": "node1"})(),
                    ],
                })(),
            })(),
        ],
    })()
    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: core)
    url = prometheus._resolve_prometheus_url(prometheus.get_settings())
    # NodePort is preferred, AND the URL is now the real externally-reachable
    # form (`<node-ip>:<node_port>`) instead of the ClusterIP URL the
    # MCP client cannot route to.
    assert url == "http://12.2.40.40:45149"


def test_resolve_hardcoded_nodeport_candidate_returns_external_url(monkeypatch):
    """When the first hardcoded candidate is itself a NodePort Service
    (e.g. kube-prometheus-stack on a cluster that pre-exposes it), step 2
    of `_resolve_prometheus_url` must also build the NodePort URL with
    Node-IP substitution — not just fall through to ClusterIP."""
    monkeypatch.delenv("K8S_MCP_PROMETHEUS_URL", raising=False)
    reset_settings_cache()

    class _Core:
        def read_namespaced_service(self, name, namespace, **kw):
            return _svc(
                "monitoring", "kube-prometheus-stack-prometheus",
                ip="10.96.10.20", port=9090,
                svc_type="NodePort", node_port=31200,
            )

        def list_node(self):
            return type("R", (), {
                "items": [
                    type("N", (), {
                        "status": type("S", (), {
                            "addresses": [
                                type("A", (), {"type": "InternalIP", "address": "10.0.0.5"})(),
                            ],
                        })(),
                    })(),
                ],
            })()

    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: _Core())
    url = prometheus._resolve_prometheus_url(prometheus.get_settings())
    assert url == "http://10.0.0.5:31200"


def test_resolve_nodeport_falls_back_to_clusterip_when_no_node(monkeypatch):
    """NodePort Service but no Node has an InternalIP (e.g. partial test
    fixture): `_external_service_url` returns None and we fall through
    to `_service_url`'s ClusterIP URL. The MCP client won't be able to
    reach it, but at least the tool returns a parseable URL and the
    agent sees a concrete failure on the HTTP call rather than a
    confusing LookupError."""
    monkeypatch.delenv("K8S_MCP_PROMETHEUS_URL", raising=False)
    reset_settings_cache()

    class _Core:
        def read_namespaced_service(self, name, namespace, **kw):
            return _svc(
                "monitoring", "kube-prometheus-stack-prometheus",
                ip="10.96.10.20", port=9090,
                svc_type="NodePort", node_port=31200,
            )

        def list_node(self):
            # Empty — no addresses found.
            return type("R", (), {"items": []})()

    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: _Core())
    url = prometheus._resolve_prometheus_url(prometheus.get_settings())
    assert url == "http://10.96.10.20:9090"


def test_resolve_wide_scan_respects_allowlist(monkeypatch):
    """When K8S_MCP_PROMETHEUS_NAMESPACE_ALLOWLIST excludes every namespace
    containing a Prometheus-looking Service, the wide scan returns nothing
    and the resolver raises LookupError — bounded by design."""
    monkeypatch.delenv("K8S_MCP_PROMETHEUS_URL", raising=False)
    monkeypatch.setenv("K8S_MCP_PROMETHEUS_NAMESPACE_ALLOWLIST", "monitoring")
    reset_settings_cache()

    core = _WideScanCore(
        namespaces=["default", "monitoring"],
        services_by_ns={
            "default": [
                _svc("default", "monitor-kube-prometheus-st-prometheus",
                     ip="10.96.3.39"),
            ],
            "monitoring": [_svc("monitoring", "kube-dns")],
        },
    )
    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: core)
    with pytest.raises(LookupError):
        prometheus._resolve_prometheus_url(prometheus.get_settings())


def test_resolve_wide_scan_allowlist_includes_match(monkeypatch):
    """Allowlist that DOES include the namespace where Prometheus lives
    → wide scan finds it and returns the URL."""
    monkeypatch.delenv("K8S_MCP_PROMETHEUS_URL", raising=False)
    monkeypatch.setenv("K8S_MCP_PROMETHEUS_NAMESPACE_ALLOWLIST", "default,monitoring")
    reset_settings_cache()

    core = _WideScanCore(
        namespaces=["default", "kube-system", "monitoring"],
        services_by_ns={
            "default": [
                _svc("default", "monitor-kube-prometheus-st-prometheus",
                     ip="10.96.3.39", port=9090),
            ],
            "kube-system": [_svc("kube-system", "kube-dns")],
            "monitoring": [_svc("monitoring", "grafana")],
        },
    )
    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: core)
    url = prometheus._resolve_prometheus_url(prometheus.get_settings())
    assert url == "http://10.96.3.39:9090"


# =============================================================================
# find_prometheus_service — allowlist filtering
# =============================================================================


def test_find_prometheus_service_respects_allowlist(monkeypatch):
    """With K8S_MCP_PROMETHEUS_NAMESPACE_ALLOWLIST set, only those
    namespaces appear in the result — even if Prometheus exists outside
    the allowlist."""
    monkeypatch.delenv("K8S_MCP_PROMETHEUS_URL", raising=False)
    monkeypatch.setenv("K8S_MCP_PROMETHEUS_NAMESPACE_ALLOWLIST", "monitoring")
    reset_settings_cache()

    core = _WideScanCore(
        namespaces=["default", "monitoring"],
        services_by_ns={
            "default": [
                _svc("default", "monitor-kube-prometheus-st-prometheus",
                     ip="10.96.3.39"),
            ],
            "monitoring": [
                _svc("monitoring", "kube-prometheus-stack-prometheus",
                     ip="10.96.20.5"),
            ],
        },
    )
    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: core)
    out = prometheus.find_prometheus_service()
    # Allowed ns shows up
    assert "kube-prometheus-stack-prometheus" in out
    assert "monitoring" in out
    # Excluded ns is filtered out
    assert "monitor-kube-prometheus-st-prometheus" not in out


def test_find_prometheus_service_explicit_namespace_bypasses_allowlist(monkeypatch):
    """An explicit `namespace=` arg is a deliberate single-ns query — it
    should NOT be silently filtered by the allowlist. (The caller knows
    what they're asking for.)"""
    monkeypatch.delenv("K8S_MCP_PROMETHEUS_URL", raising=False)
    monkeypatch.setenv("K8S_MCP_PROMETHEUS_NAMESPACE_ALLOWLIST", "monitoring")
    reset_settings_cache()

    class _Core:
        def list_namespaced_service(self, namespace, **kw):
            assert namespace == "default"
            return _SvcList([
                _svc("default", "monitor-kube-prometheus-st-prometheus",
                     ip="10.96.3.39"),
            ])

    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: _Core())
    out = prometheus.find_prometheus_service(namespace="default")
    assert "monitor-kube-prometheus-st-prometheus" in out
    assert "default" in out


def test_find_prometheus_service_empty_allowlist_surfaces_allowlist_in_message(monkeypatch):
    """When the allowlist excludes every namespace, the empty-result
    notice must mention the allowlist so the user knows why they got
    nothing (vs. the default 'no Prometheus installed' message)."""
    monkeypatch.delenv("K8S_MCP_PROMETHEUS_URL", raising=False)
    monkeypatch.setenv("K8S_MCP_PROMETHEUS_NAMESPACE_ALLOWLIST", "monitoring")
    reset_settings_cache()

    core = _WideScanCore(
        namespaces=["default"],
        services_by_ns={
            "default": [
                _svc("default", "monitor-kube-prometheus-st-prometheus",
                     ip="10.96.3.39"),
            ],
        },
    )
    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: core)
    out = prometheus.find_prometheus_service()
    assert "allowlist" in out.lower()
    assert "monitoring" in out


# =============================================================================
# _wide_scan_prometheus_matches — namespace parameter (C5 merge)
# =============================================================================


def test_wide_scan_with_namespace_uses_namespaced_list(monkeypatch):
    """When `namespace` is passed, the helper uses list_namespaced_service
    instead of list_service_for_all_namespaces — cheaper, and the test
    also verifies the cluster-wide list is NOT called."""
    monkeypatch.delenv("K8S_MCP_PROMETHEUS_NAMESPACE_ALLOWLIST", raising=False)
    reset_settings_cache()

    called = {"namespaced": [], "all_ns": 0}

    class _Core:
        def list_namespaced_service(self, namespace, **kw):
            called["namespaced"].append(namespace)
            return _SvcList([
                _svc(namespace, "prometheus",
                     ip="10.96.1.1", port=9090, svc_type="ClusterIP"),
            ])

        def list_service_for_all_namespaces(self, **kw):
            called["all_ns"] += 1
            return _SvcList([])

    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: _Core())
    pairs = prometheus._wide_scan_prometheus_matches(
        _Core(), prometheus.get_settings(), namespace="monitoring",
    )
    assert called["namespaced"] == ["monitoring"]
    assert called["all_ns"] == 0
    assert len(pairs) == 1
    assert pairs[0][0] == "monitoring"


def test_wide_scan_with_namespace_bypasses_allowlist(monkeypatch):
    """Allowlist is meaningless for an explicit namespace request —
    caller is naming the namespace, not bounding the search surface.
    If allowlist were applied, the explicit request could be silently
    dropped, which is the opposite of what the agent asked for."""
    monkeypatch.setenv("K8S_MCP_PROMETHEUS_NAMESPACE_ALLOWLIST", "other-ns")
    reset_settings_cache()

    class _Core:
        def list_namespaced_service(self, namespace, **kw):
            # Namespace != allowlist but caller asked for it explicitly —
            # we should still return the result.
            return _SvcList([
                _svc(namespace, "prometheus",
                     ip="10.96.1.1", port=9090, svc_type="ClusterIP"),
            ])

    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: _Core())
    pairs = prometheus._wide_scan_prometheus_matches(
        _Core(), prometheus.get_settings(), namespace="monitoring",
    )
    assert len(pairs) == 1
    assert pairs[0][0] == "monitoring"


def test_wide_scan_cluster_wide_still_honors_allowlist(monkeypatch):
    """Cluster-wide mode keeps the existing allowlist behavior — this
    is the regression pin for the merge."""
    monkeypatch.setenv("K8S_MCP_PROMETHEUS_NAMESPACE_ALLOWLIST", "monitoring")
    reset_settings_cache()

    services = [
        _svc("monitoring", "prometheus",
             ip="10.96.1.1", port=9090, svc_type="ClusterIP"),
        _svc("other", "prometheus-other",
             ip="10.96.2.2", port=9090, svc_type="ClusterIP"),
    ]

    class _Core:
        def list_service_for_all_namespaces(self, **kw):
            return _SvcList(services)

        def list_namespaced_service(self, namespace, **kw):
            return _SvcList([])

    monkeypatch.setattr(prometheus.client, "CoreV1Api", lambda: _Core())
    pairs = prometheus._wide_scan_prometheus_matches(
        _Core(), prometheus.get_settings(),
    )
    # Allowlist drops the "other" ns service; only monitoring survives.
    assert [p[0] for p in pairs] == ["monitoring"]
