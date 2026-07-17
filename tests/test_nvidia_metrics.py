from __future__ import annotations

from datetime import UTC, datetime

import pytest

from k8s_mcp.tools import nvidia_metrics as gpu_metrics


def _sample(labels: dict[str, str], value: str) -> dict:
    return {"metric": labels, "value": ["1710000000", value]}


def test_gpu_metrics_catalog_discovers_available_dcgm_metrics(monkeypatch):
    seen: list[tuple[str, str | None]] = []

    def query(promql, prometheus_url=None):
        seen.append((promql, prometheus_url))
        return [
            _sample({"__name__": "DCGM_FI_DEV_GPU_UTIL"}, "4"),
            _sample({"__name__": "DCGM_FI_DEV_FB_USED"}, "4"),
        ]

    monkeypatch.setattr(gpu_metrics, "_query_instant", query)
    report = gpu_metrics.gpu_metrics_catalog(prometheus_url="http://prometheus:9090")

    assert '__name__=~"DCGM_.*"' in seen[0][0]
    assert seen[0][1] == "http://prometheus:9090"
    assert "Metrics found: 2" in report
    assert report.index("DCGM_FI_DEV_FB_USED") < report.index("DCGM_FI_DEV_GPU_UTIL")


def test_gpu_metrics_catalog_handles_missing_prometheus_and_validation(monkeypatch):
    monkeypatch.setattr(gpu_metrics, "_query_instant", lambda *args: (_ for _ in ()).throw(LookupError("no endpoint")))

    report = gpu_metrics.gpu_metrics_catalog()

    assert "Prometheus metric query unavailable: no endpoint" in report
    assert "find_prometheus_service" in report
    with pytest.raises(ValueError, match="metric_prefix"):
        gpu_metrics.gpu_metrics_catalog(metric_prefix="DCGM_.*")
    with pytest.raises(ValueError, match="limit must be"):
        gpu_metrics.gpu_metrics_catalog(limit=0)


def test_gpu_utilization_overview_merges_standard_dcgm_samples(monkeypatch):
    samples = {
        "DCGM_FI_DEV_GPU_UTIL": [
            _sample({"Hostname": "gpu-1", "gpu": "0", "UUID": "GPU-a"}, "80"),
            _sample({"Hostname": "gpu-1", "gpu": "1", "UUID": "GPU-b"}, "20"),
        ],
        "DCGM_FI_DEV_FB_USED": [
            _sample({"Hostname": "gpu-1", "gpu": "0", "UUID": "GPU-a"}, "4000"),
            _sample({"Hostname": "gpu-1", "gpu": "1", "UUID": "GPU-b"}, "1000"),
        ],
        "DCGM_FI_DEV_FB_TOTAL": [
            _sample({"Hostname": "gpu-1", "gpu": "0", "UUID": "GPU-a"}, "8000"),
            _sample({"Hostname": "gpu-1", "gpu": "1", "UUID": "GPU-b"}, "8000"),
        ],
    }
    monkeypatch.setattr(gpu_metrics, "_query_instant", lambda promql, prometheus_url=None: samples[promql])

    report = gpu_metrics.gpu_utilization_overview()

    assert "Hostname=gpu-1, gpu=0, UUID=GPU-a" in report
    assert "80" in report
    assert "50.0%" in report
    assert "12.5%" in report


def test_gpu_utilization_overview_reports_missing_metrics_without_hiding_available_values(monkeypatch):
    def query(promql, prometheus_url=None):
        if promql == "DCGM_FI_DEV_GPU_UTIL":
            return [_sample({"Hostname": "gpu-1", "gpu": "0"}, "70")]
        return []

    monkeypatch.setattr(gpu_metrics, "_query_instant", query)
    report = gpu_metrics.gpu_utilization_overview()

    assert "Hostname=gpu-1, gpu=0" in report
    assert "No samples for: MEMORY_USED" in report
    assert "gpu_metrics_catalog" in report
    with pytest.raises(ValueError, match="metric_name"):
        gpu_metrics.gpu_utilization_overview(utilization_metric="DCGM_FI.*")


def test_gpu_workload_utilization_uses_exact_escaped_pod_labels(monkeypatch):
    seen: list[str] = []

    def query(promql, prometheus_url=None):
        seen.append(promql)
        return [_sample({"Hostname": "gpu-1", "gpu": "0", "container": "trainer"}, "94")]

    monkeypatch.setattr(gpu_metrics, "_query_instant", query)
    report = gpu_metrics.gpu_workload_utilization('train"er', 'ml\\team')

    assert 'namespace="ml\\\\team"' in seen[0]
    assert 'pod="train\\"er"' in seen[0]
    assert "container" in report.lower()
    assert "94" in report



def test_gpu_utilization_history_summarizes_bounded_range(monkeypatch):
    captured = {}

    def query(promql, start, end, step, prometheus_url=None):
        captured.update(promql=promql, start=start, end=end, step=step, url=prometheus_url)
        return [
            {
                "metric": {"Hostname": "gpu-1", "gpu": "0", "UUID": "GPU-a"},
                "values": [[1, "10"], [2, "30"], [3, "20"]],
            },
            {
                "metric": {"Hostname": "gpu-1", "gpu": "1", "UUID": "GPU-b"},
                "values": [[1, "80"], [2, "100"]],
            },
        ]

    monkeypatch.setattr(gpu_metrics, "_utc_now", lambda: datetime(2026, 7, 17, 4, 0, tzinfo=UTC))
    monkeypatch.setattr(gpu_metrics, "_query_range", query)

    report = gpu_metrics.gpu_utilization_history(duration="1h", step="5m", prometheus_url="http://prom:9090")

    assert captured == {
        "promql": "DCGM_FI_DEV_GPU_UTIL",
        "start": "2026-07-17T03:00:00Z",
        "end": "2026-07-17T04:00:00Z",
        "step": "5m",
        "url": "http://prom:9090",
    }
    assert "Series with finite samples: 2" in report
    assert "GPU-b" in report
    assert "90" in report
    assert report.index("GPU-b") < report.index("GPU-a")


def test_gpu_utilization_history_escapes_exact_workload_labels(monkeypatch):
    seen = []

    def query(promql, start, end, step, prometheus_url=None):
        seen.append(promql)
        return []

    monkeypatch.setattr(gpu_metrics, "_query_range", query)
    report = gpu_metrics.gpu_utilization_history(namespace="ml\\team", pod_name='train"er')

    assert 'namespace="ml\\\\team"' in seen[0]
    assert 'pod="train\\"er"' in seen[0]
    assert "No finite GPU samples" in report


def test_gpu_utilization_history_enforces_query_bounds():
    with pytest.raises(ValueError, match="must not exceed 7d"):
        gpu_metrics.gpu_utilization_history(duration="8d")
    with pytest.raises(ValueError, match="at least 15s"):
        gpu_metrics.gpu_utilization_history(step="10s")
    with pytest.raises(ValueError, match="at most 1000 points"):
        gpu_metrics.gpu_utilization_history(duration="7d", step="5m")
    with pytest.raises(ValueError, match="namespace is required"):
        gpu_metrics.gpu_utilization_history(pod_name="trainer")
    with pytest.raises(ValueError, match="limit must be"):
        gpu_metrics.gpu_utilization_history(limit=101)


def test_gpu_utilization_history_handles_prometheus_error_and_nonfinite_series(monkeypatch):
    monkeypatch.setattr(
        gpu_metrics,
        "_query_range",
        lambda *args: (_ for _ in ()).throw(LookupError("no endpoint")),
    )
    unavailable = gpu_metrics.gpu_utilization_history()
    assert "Prometheus metric query unavailable: no endpoint" in unavailable

    monkeypatch.setattr(
        gpu_metrics,
        "_query_range",
        lambda *args: [{"metric": {"gpu": "0"}, "values": [[1, "NaN"], [2, "Inf"]]}],
    )
    report = gpu_metrics.gpu_utilization_history()
    assert "Skipped 1 series" in report
