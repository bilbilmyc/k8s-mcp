"""Read-only NVIDIA GPU metrics backed by Prometheus/DCGM Exporter.

The tools in this module intentionally avoid Kubernetes mutations. Metric names
are configurable because exporter versions and recording rules can differ
between clusters.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from ..formatters import short_table
from .prometheus import _query_instant, _query_range

_METRIC_NAME_RE = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*\Z")
_METRIC_PREFIX_RE = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*\Z")
_DURATION_RE = re.compile(r"([1-9][0-9]*)([smhd])\Z")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_MAX_HISTORY_SECONDS = 7 * 86400
_MAX_HISTORY_POINTS = 1000


def _validated_metric_name(metric_name: str) -> str:
    """Validate a Prometheus metric identifier before embedding it in PromQL."""
    if not _METRIC_NAME_RE.fullmatch(metric_name):
        raise ValueError(
            "metric_name must be a Prometheus metric identifier "
            "(letters, digits, underscores, colons; cannot start with a digit)"
        )
    return metric_name


def _promql_string(value: str) -> str:
    """Quote a PromQL label value without permitting matcher injection."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _series_value(series: dict[str, Any]) -> tuple[str, float | None]:
    value = series.get("value") or []
    raw = str(value[1]) if isinstance(value, list) and len(value) >= 2 else "-"
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return raw, None
    return raw, number if math.isfinite(number) else None


def _gpu_series_identity(labels: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """Build a stable enough per-GPU key across common DCGM label variants."""
    keys = ("Hostname", "hostname", "host", "node", "instance", "gpu", "UUID", "uuid", "device")
    identity = tuple((key, str(labels[key])) for key in keys if labels.get(key) not in (None, ""))
    if identity:
        return identity
    ignored = {"__name__", "job", "endpoint", "service"}
    return tuple(sorted((str(key), str(value)) for key, value in labels.items() if key not in ignored))


def _render_gpu_identity(identity: tuple[tuple[str, str], ...]) -> str:
    return ", ".join(f"{key}={value}" for key, value in identity) or "unlabeled GPU series"


def _prometheus_unavailable(title: str, exc: Exception) -> str:
    return (
        f"## {title}\n\n"
        f"Prometheus metric query unavailable: {exc}\n\n"
        "Set `K8S_MCP_PROMETHEUS_URL`, pass `prometheus_url`, or call "
        "`find_prometheus_service()` to locate a reachable Prometheus endpoint."
    )


def gpu_metrics_catalog(
    metric_prefix: str = "DCGM_",
    limit: int = 100,
    prometheus_url: str | None = None,
) -> str:
    """📚 LIST NVIDIA GPU METRICS — discover DCGM metric names available in Prometheus.

    Args:
        metric_prefix: metric-name prefix to discover (default `DCGM_`).
        limit: maximum metric names to render (1-500, default 100).
        prometheus_url: optional explicit URL; otherwise uses the existing
            Prometheus environment/discovery configuration.

    This uses a read-only PromQL metadata query and reports the metric names
    actually stored in the target Prometheus. Use it before selecting custom
    metric names for other GPU observability tools.
    """
    if not _METRIC_PREFIX_RE.fullmatch(metric_prefix):
        raise ValueError("metric_prefix must contain only metric identifier characters")
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    promql = f'count by (__name__) ({{__name__=~"{_promql_string(metric_prefix)}.*"}})'
    try:
        series = _query_instant(promql, prometheus_url)
    except (LookupError, ValueError) as exc:
        return _prometheus_unavailable("NVIDIA GPU metric catalog", exc)

    rows = []
    for result in series:
        labels = result.get("metric") or {}
        metric_name = str(labels.get("__name__", "<unknown>"))
        raw_count, _ = _series_value(result)
        rows.append({"METRIC": metric_name, "SERIES": raw_count})
    rows.sort(key=lambda row: row["METRIC"])
    shown = rows[:limit]
    lines = [
        "## NVIDIA GPU metric catalog",
        f"Prefix: `{metric_prefix}` | Metrics found: {len(rows)} | Shown: {len(shown)}",
        short_table(shown, ["METRIC", "SERIES"]),
    ]
    if not rows:
        lines.append(
            "No matching metrics were returned. Confirm that DCGM Exporter is scraped by this Prometheus, "
            "or pass the metric prefix used by your exporter."
        )
    elif len(rows) > len(shown):
        lines.append(f"Truncated at limit={limit}; rerun with a higher limit (max 500).")
    return "\n".join(lines)


def gpu_utilization_overview(
    utilization_metric: str = "DCGM_FI_DEV_GPU_UTIL",
    memory_used_metric: str = "DCGM_FI_DEV_FB_USED",
    memory_total_metric: str = "DCGM_FI_DEV_FB_TOTAL",
    prometheus_url: str | None = None,
) -> str:
    """📊 NVIDIA GPU UTILIZATION OVERVIEW — show latest per-GPU DCGM metric samples.

    Args:
        utilization_metric: Prometheus gauge for GPU utilization. Default is
            the common DCGM Exporter `DCGM_FI_DEV_GPU_UTIL` metric.
        memory_used_metric: Prometheus gauge for framebuffer memory used.
        memory_total_metric: Prometheus gauge for framebuffer memory total.
        prometheus_url: optional explicit Prometheus URL.

    The tool reads raw instant-vector samples and does not assume a GPU SKU,
    a fixed label schema, or a particular unit conversion. It matches common
    GPU identity labels (`Hostname`, `gpu`, `UUID`, and variants) across the
    selected metrics. Use `gpu_metrics_catalog()` to discover custom names.
    """
    metrics = {
        "UTILIZATION": _validated_metric_name(utilization_metric),
        "MEMORY_USED": _validated_metric_name(memory_used_metric),
        "MEMORY_TOTAL": _validated_metric_name(memory_total_metric),
    }
    results: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}
    for column, metric_name in metrics.items():
        try:
            results[column] = _query_instant(metric_name, prometheus_url)
        except (LookupError, ValueError) as exc:
            errors[column] = str(exc)
            results[column] = []

    if len(errors) == len(metrics):
        return _prometheus_unavailable("NVIDIA GPU utilization overview", ValueError(next(iter(errors.values()))))

    rows_by_gpu: dict[tuple[tuple[str, str], ...], dict[str, Any]] = {}
    numeric_values: dict[tuple[tuple[str, str], ...], dict[str, float]] = defaultdict(dict)
    for column, samples in results.items():
        for sample in samples:
            labels = sample.get("metric") or {}
            identity = _gpu_series_identity(labels)
            row = rows_by_gpu.setdefault(
                identity,
                {
                    "GPU": _render_gpu_identity(identity),
                    "UTILIZATION": "-",
                    "MEMORY_USED": "-",
                    "MEMORY_TOTAL": "-",
                    "MEMORY_USED_RATIO": "-",
                },
            )
            raw, number = _series_value(sample)
            old_number = numeric_values[identity].get(column)
            if old_number is None or (number is not None and number > old_number):
                row[column] = raw
                if number is not None:
                    numeric_values[identity][column] = number

    for identity, row in rows_by_gpu.items():
        used = numeric_values[identity].get("MEMORY_USED")
        total = numeric_values[identity].get("MEMORY_TOTAL")
        if used is not None and total is not None and total > 0:
            row["MEMORY_USED_RATIO"] = f"{used / total * 100:.1f}%"

    rows = [rows_by_gpu[key] for key in sorted(rows_by_gpu, key=_render_gpu_identity)]
    lines = [
        "## NVIDIA GPU utilization overview",
        "Metric mapping: " + ", ".join(f"{column.lower()}=`{name}`" for column, name in metrics.items()),
        "Values are the latest raw Prometheus samples; units follow the selected exporter metrics.",
        short_table(rows, ["GPU", "UTILIZATION", "MEMORY_USED", "MEMORY_TOTAL", "MEMORY_USED_RATIO"]),
    ]
    missing = [f"{column} (`{metrics[column]}`)" for column, samples in results.items() if not samples]
    if missing:
        lines.append("No samples for: " + ", ".join(missing) + ". Use `gpu_metrics_catalog()` to select available metric names.")
    if errors:
        lines.append("Query errors: " + "; ".join(f"{column}: {message}" for column, message in errors.items()))
    if not rows:
        lines.append("No GPU metric series were returned. Confirm DCGM Exporter is scraped by the selected Prometheus.")
    return "\n".join(lines)


def gpu_workload_utilization(
    pod_name: str,
    namespace: str = "default",
    metric_name: str = "DCGM_FI_DEV_GPU_UTIL",
    prometheus_url: str | None = None,
) -> str:
    """📈 INSPECT GPU WORKLOAD UTILIZATION — show GPU metric samples attributed to one Pod.

    Args:
        pod_name: exact Kubernetes Pod name expected in the Prometheus `pod` label.
        namespace: exact Kubernetes namespace expected in the `namespace` label.
        metric_name: DCGM or exporter metric to read (default GPU utilization).
        prometheus_url: optional explicit Prometheus URL.

    This tool is read-only. It requires the selected metric to carry Kubernetes
    `namespace` and `pod` labels; if the DCGM Exporter scrape configuration does
    not attach them, use `gpu_utilization_overview()` for node/GPU-level data.
    """
    metric_name = _validated_metric_name(metric_name)
    if not pod_name:
        raise ValueError("pod_name must not be empty")
    if not namespace:
        raise ValueError("namespace must not be empty")
    promql = (
        f'{metric_name}{{namespace="{_promql_string(namespace)}",'
        f'pod="{_promql_string(pod_name)}"}}'
    )
    try:
        samples = _query_instant(promql, prometheus_url)
    except (LookupError, ValueError) as exc:
        return _prometheus_unavailable("NVIDIA GPU workload utilization", exc)

    rows = []
    for sample in samples:
        labels = sample.get("metric") or {}
        raw, _ = _series_value(sample)
        rows.append(
            {
                "GPU": _render_gpu_identity(_gpu_series_identity(labels)),
                "CONTAINER": str(labels.get("container") or labels.get("container_name") or "-"),
                "VALUE": raw,
            }
        )
    rows.sort(key=lambda row: (row["GPU"], row["CONTAINER"]))
    lines = [
        f"## NVIDIA GPU workload utilization — {namespace}/{pod_name}",
        f"Metric: `{metric_name}` | Samples: {len(rows)}",
        "Values are the latest raw Prometheus samples; units follow the selected exporter metric.",
        short_table(rows, ["GPU", "CONTAINER", "VALUE"]),
    ]
    if not rows:
        lines.append(
            "No matching series. The metric may be absent, the Pod may not currently emit it, or the DCGM "
            "Exporter metric lacks `namespace`/`pod` labels. Use `gpu_utilization_overview()` or "
            "`gpu_metrics_catalog()` next."
        )
    return "\n".join(lines)


def _duration_seconds(value: str, field: str) -> int:
    match = _DURATION_RE.fullmatch(value)
    if not match:
        raise ValueError(f"{field} must be an integer Prometheus duration using s, m, h, or d")
    return int(match.group(1)) * _DURATION_UNITS[match.group(2)]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _history_stats(values: list[Any]) -> tuple[int, float, float, float, float] | None:
    numbers: list[float] = []
    for point in values:
        if not isinstance(point, list | tuple) or len(point) < 2:
            continue
        try:
            number = float(point[1])
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            numbers.append(number)
    if not numbers:
        return None
    return len(numbers), min(numbers), sum(numbers) / len(numbers), max(numbers), numbers[-1]


def gpu_utilization_history(
    duration: str = "1h",
    step: str = "5m",
    metric_name: str = "DCGM_FI_DEV_GPU_UTIL",
    namespace: str | None = None,
    pod_name: str | None = None,
    limit: int = 20,
    prometheus_url: str | None = None,
) -> str:
    """📉 NVIDIA GPU UTILIZATION HISTORY — summarize a bounded Prometheus time window.

    Args:
        duration: lookback duration using an integer plus `s`, `m`, `h`, or `d`.
            The maximum window is 7d.
        step: Prometheus range-query resolution. It must be at least 15s and
            produce no more than 1000 points per series.
        metric_name: GPU utilization metric to query.
        namespace: optional exact Prometheus `namespace` label filter.
        pod_name: optional exact `pod` label filter; requires `namespace`.
        limit: maximum series rendered (1-100, default 20).
        prometheus_url: optional explicit Prometheus URL.

    The tool is read-only and returns per-series sample count, minimum,
    average, maximum, and latest finite values instead of dumping every point.
    Units follow the selected exporter metric.
    """
    metric_name = _validated_metric_name(metric_name)
    duration_seconds = _duration_seconds(duration, "duration")
    step_seconds = _duration_seconds(step, "step")
    if duration_seconds > _MAX_HISTORY_SECONDS:
        raise ValueError("duration must not exceed 7d")
    if step_seconds < 15:
        raise ValueError("step must be at least 15s")
    if duration_seconds / step_seconds > _MAX_HISTORY_POINTS:
        raise ValueError("duration/step must produce at most 1000 points per series")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if pod_name and not namespace:
        raise ValueError("namespace is required when pod_name is set")
    if namespace == "":
        raise ValueError("namespace must not be empty")
    if pod_name == "":
        raise ValueError("pod_name must not be empty")

    matchers = []
    if namespace is not None:
        matchers.append(f'namespace="{_promql_string(namespace)}"')
    if pod_name is not None:
        matchers.append(f'pod="{_promql_string(pod_name)}"')
    selector = "{" + ",".join(matchers) + "}" if matchers else ""
    promql = metric_name + selector
    end_time = _utc_now()
    start_time = end_time - timedelta(seconds=duration_seconds)
    start = _rfc3339(start_time)
    end = _rfc3339(end_time)

    try:
        series = _query_range(promql, start, end, step, prometheus_url)
    except (LookupError, ValueError) as exc:
        return _prometheus_unavailable("NVIDIA GPU utilization history", exc)

    rows = []
    skipped = 0
    for result in series:
        stats = _history_stats(result.get("values") or [])
        if stats is None:
            skipped += 1
            continue
        samples, minimum, average, maximum, latest = stats
        labels = result.get("metric") or {}
        rows.append(
            {
                "GPU": _render_gpu_identity(_gpu_series_identity(labels)),
                "POD": str(labels.get("pod") or "-"),
                "CONTAINER": str(labels.get("container") or labels.get("container_name") or "-"),
                "SAMPLES": str(samples),
                "MIN": f"{minimum:.3g}",
                "AVG": f"{average:.3g}",
                "MAX": f"{maximum:.3g}",
                "LATEST": f"{latest:.3g}",
            }
        )
    rows.sort(key=lambda row: (-float(row["AVG"]), row["GPU"], row["POD"], row["CONTAINER"]))
    shown = rows[:limit]
    lines = [
        "## NVIDIA GPU utilization history",
        f"Metric: `{metric_name}` | Window: {start} → {end} | Step: {step}",
        f"Series with finite samples: {len(rows)} | Shown: {len(shown)}",
        "Values are raw exporter samples summarized over the selected window.",
        short_table(shown, ["GPU", "POD", "CONTAINER", "SAMPLES", "MIN", "AVG", "MAX", "LATEST"]),
    ]
    if not rows:
        lines.append(
            "No finite GPU samples were returned. Confirm the metric and labels with `gpu_metrics_catalog()` "
            "or inspect current samples with `gpu_utilization_overview()`."
        )
    if len(rows) > len(shown):
        lines.append(f"Truncated at limit={limit}; {len(rows) - len(shown)} additional series omitted.")
    if skipped:
        lines.append(f"Skipped {skipped} series without finite numeric samples.")
    return "\n".join(lines)


def register(mcp) -> None:
    mcp.tool()(gpu_metrics_catalog)
    mcp.tool()(gpu_utilization_overview)
    mcp.tool()(gpu_workload_utilization)
    mcp.tool()(gpu_utilization_history)
