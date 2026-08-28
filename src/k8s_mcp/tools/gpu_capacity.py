"""Read-only GPU capacity analysis: Kubernetes reservations × Prometheus windows.

This module joins the two other GPU views:
  - `nvidia_gpu` knows what Kubernetes exposes (allocatable `nvidia.com/*`
    resources, Pod GPU limits, placement);
  - `nvidia_metrics` knows what Prometheus/DCGM actually measured.

`gpu_capacity_analyze` answers "does requested capacity match real
utilization, and is what remains fragmented?"; `gpu_idle_resources` lists
the GPU series that sat below an idle threshold over a window. Both are
read-only and never mutate cluster objects or Prometheus.
"""
from __future__ import annotations

from typing import Any

from ..formatters import short_table
from .nvidia_gpu import (
    _core_v1,
    _format_quantity,
    _is_gpu_node,
    _items,
    _list_gpu_pods,
    _list_pods,
    _name,
    _node_gpu_resources,
    _pod_gpu_limits,
    _pod_node,
    _pod_phase,
    _quantity,
)
from .nvidia_metrics import (
    _MAX_HISTORY_SECONDS,
    _duration_seconds,
    _gpu_series_identity,
    _prometheus_unavailable,
    _render_gpu_identity,
    _series_value,
    _validated_metric_name,
)
from .prometheus import _query_instant

# Per-GPU series carry exporter-specific identity labels; these are the
# keys whose value is usually the Kubernetes Node name on daemonset-style
# DCGM deployments. Series that match no known node land in an "unmatched"
# bucket instead of being silently dropped.
_NODE_LABEL_CANDIDATES = ("Hostname", "hostname", "host", "node", "kubernetes_node", "node_name")
_MIG_RESOURCE_PREFIX = "nvidia.com/mig"


def _gpu_units(resources: dict[str, str]) -> float:
    """Count GPU units as whole GPUs plus MIG slices (approximate on
    mixed-strategy nodes that expose both; that is the unit Prometheus
    series also correspond to)."""
    total = _quantity(resources.get("nvidia.com/gpu", "0"))
    total += sum(_quantity(v) for k, v in resources.items() if k.startswith(_MIG_RESOURCE_PREFIX))
    return total


def _k8s_gpu_view() -> tuple[dict[str, dict[str, float]], int]:
    """Per-GPU-node allocatable units and units held by scheduled Pods.

    Returns ({node_name: {"allocatable": u, "held": u}}, pending_gpu_pods).
    GPU Pods on non-GPU nodes or still Pending are counted in the second
    element — they consume nothing yet but demonstrate unmet demand.
    """
    core = _core_v1()
    nodes = [node for node in _items(core.list_node()) if _is_gpu_node(node)]
    per_node: dict[str, dict[str, float]] = {}
    for node in nodes:
        _, allocatable = _node_gpu_resources(node)
        per_node[_name(node)] = {"allocatable": _gpu_units(allocatable), "held": 0.0}

    pending = 0
    pods = [pod for pod in _list_gpu_pods(_list_pods(core)) if _pod_phase(pod) not in {"Succeeded", "Failed"}]
    for pod in pods:
        node = _pod_node(pod)
        held = _gpu_units(_pod_gpu_limits(pod))
        if node in per_node:
            per_node[node]["held"] += held
        elif _pod_phase(pod) == "Pending":
            pending += 1
    return per_node, pending


def _window_stats(
    metric_name: str, duration: str, prometheus_url: str | None,
) -> dict[tuple[tuple[str, str], ...], dict[str, float | None]]:
    """Per-series AVG/MAX over the window via two instant queries.

    `avg_over_time`/`max_over_time` collapse the window into one sample per
    series, so no range-query point budget applies. Keyed by the series
    identity used across the GPU metrics tools.
    """
    avg_series = _query_instant(f"avg_over_time({metric_name}[{duration}])", prometheus_url)
    max_series = _query_instant(f"max_over_time({metric_name}[{duration}])", prometheus_url)

    def _value_of(entries: list[dict[str, Any]]) -> dict[tuple[tuple[str, str], ...], float | None]:
        out: dict[tuple[tuple[str, str], ...], float | None] = {}
        for entry in entries:
            _, number = _series_value(entry)
            out[_gpu_series_identity(entry.get("metric") or {})] = number
        return out

    avgs = _value_of(avg_series)
    peaks = _value_of(max_series)
    return {
        identity: {"avg": avgs.get(identity), "max": peaks.get(identity)}
        for identity in avgs.keys() | peaks.keys()
    }


def _series_node(labels: dict[str, Any], node_names: set[str]) -> str | None:
    for key in _NODE_LABEL_CANDIDATES:
        value = labels.get(key)
        if value and str(value) in node_names:
            return str(value)
    return None


def _render_util(value: float | None) -> str:
    return f"{value:.3g}" if value is not None else "-"


def gpu_capacity_analyze(
    duration: str = "1h",
    metric_name: str = "DCGM_FI_DEV_GPU_UTIL",
    idle_threshold: float = 10.0,
    limit: int = 50,
    prometheus_url: str | None = None,
) -> str:
    """⚖️ ANALYZE GPU CAPACITY — match Kubernetes GPU reservations against real utilization.

    Args:
        duration: utilization lookback (integer + s/m/h/d, max 7d).
        metric_name: utilization metric; units are the metric's own scale
            (DCGM_FI_DEV_GPU_UTIL is percent).
        idle_threshold: a node whose GPUs are held but whose window average
            sits below this value is flagged reserved-but-idle (0-100).
        limit: maximum node rows rendered (1-200, default 50).
        prometheus_url: optional explicit Prometheus URL.

    Read-only correlation: Kubernetes allocatable units and Pod-held limits
    per node, joined with per-GPU window AVG/MAX. Findings call out
    reserved-but-idle nodes, free units stranded next to Pending GPU Pods,
    and exporter series that could not be matched to a node.
    """
    metric_name = _validated_metric_name(metric_name)
    if _duration_seconds(duration, "duration") > _MAX_HISTORY_SECONDS:
        raise ValueError("duration must not exceed 7d")
    if not 0 < idle_threshold <= 100:
        raise ValueError("idle_threshold must be within (0, 100]")
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")

    per_node, pending = _k8s_gpu_view()
    try:
        stats = _window_stats(metric_name, duration, prometheus_url)
    except (LookupError, ValueError) as exc:
        return _prometheus_unavailable("GPU capacity analysis", exc)

    by_node: dict[str | None, list[dict[str, float | None]]] = {node: [] for node in per_node}
    unmatched: list[dict[str, float | None]] = []
    for identity, entry in stats.items():
        node = _series_node(dict(identity), set(per_node))
        target = by_node[node] if node else unmatched
        target.append(entry)

    rows: list[dict[str, Any]] = []
    for node, view in sorted(per_node.items()):
        series = by_node.get(node) or []
        measured = [entry["avg"] for entry in series if entry["avg"] is not None]
        peaks = [entry["max"] for entry in series if entry["max"] is not None]
        avg = sum(measured) / len(measured) if measured else None
        rows.append(
            {
                "NODE": node,
                "GPU_UNITS": _format_quantity(view["allocatable"]),
                "HELD": _format_quantity(view["held"]),
                "SERIES": str(len(series)),
                "AVG_UTIL": _render_util(avg),
                "MAX_UTIL": _render_util(max(peaks) if peaks else None),
                "_held": view["held"],
                "_avg": avg,
            }
        )
    rows.sort(key=lambda row: row["NODE"])
    shown = rows[:limit]

    lines = ["## GPU capacity analysis", f"Window: {duration} | metric: {metric_name}"]
    lines.append(short_table(
        [{k: v for k, v in row.items() if not k.startswith("_")} for row in shown],
        ["NODE", "GPU_UNITS", "HELD", "SERIES", "AVG_UTIL", "MAX_UTIL"],
    ))

    findings: list[tuple[str, str]] = []
    for row in rows:
        if row["_held"] > 0 and row["_avg"] is not None and row["_avg"] < idle_threshold:
            findings.append(("WARN", f"Node {row['NODE']} holds {row['HELD']} GPU unit(s) but averaged "
                                     f"{_render_util(row['_avg'])} over {duration} — reserved-but-idle candidate."))
    free_units = sum(max(view["allocatable"] - view["held"], 0.0) for view in per_node.values())
    if pending and free_units:
        findings.append(("WARN", f"{pending} Pending GPU Pod(s) while {free_units:g} GPU unit(s) sit "
                                 "unheld on GPU nodes; check taints/affinity or profile mismatch "
                                 "(gpu_mig_overview, gpu_pending_workloads)."))
    if not stats:
        findings.append(("INFO", f"No {metric_name} series found in the window; run gpu_metrics_catalog "
                                 "to discover the metric names your exporter publishes."))
    elif unmatched:
        findings.append(("INFO", f"{len(unmatched)} series could not be matched to a Kubernetes Node "
                                 "via Hostname/node labels; per-node rows may undercount."))
    if not findings:
        findings.append(("OK", "Reservations and utilization line up; no idle or stranded capacity detected."))
    lines.append("\n### Findings")
    lines.extend(f"- **{level}** — {message}" for level, message in findings)
    if len(rows) > len(shown):
        lines.append(f"Truncated at limit={limit}; rerun with a higher limit (max 200).")
    return "\n".join(lines)


def gpu_idle_resources(
    duration: str = "24h",
    metric_name: str = "DCGM_FI_DEV_GPU_UTIL",
    threshold: float = 10.0,
    limit: int = 50,
    prometheus_url: str | None = None,
) -> str:
    """🛋️ LIST IDLE GPU RESOURCES — GPUs below an idle threshold over a window.

    Args:
        duration: lookback (integer + s/m/h/d, max 7d). Longer windows make
            an "idle" verdict harder to fake with a single quiet hour.
        metric_name: utilization metric; threshold uses the metric's own
            scale (DCGM_FI_DEV_GPU_UTIL is percent).
        threshold: window average below this value counts as idle (0-100).
        limit: maximum GPU rows rendered (1-200, default 50).
        prometheus_url: optional explicit Prometheus URL.

    Rows are per exporter GPU series (node + device identity), sorted
    quietest first, with the window peak kept visible so bursty-but-
    low-average GPUs are not mistaken for dead ones. Node rollups show
    whether Pods hold units on the same nodes — reserved-but-idle is the
    consolidation signal; unheld idle GPUs are the scale-down signal.
    Read-only; it deletes or reschedules nothing.
    """
    metric_name = _validated_metric_name(metric_name)
    if _duration_seconds(duration, "duration") > _MAX_HISTORY_SECONDS:
        raise ValueError("duration must not exceed 7d")
    if not 0 < threshold <= 100:
        raise ValueError("threshold must be within (0, 100]")
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")

    per_node, _pending = _k8s_gpu_view()
    try:
        stats = _window_stats(metric_name, duration, prometheus_url)
    except (LookupError, ValueError) as exc:
        return _prometheus_unavailable("Idle GPU resources", exc)

    node_names = set(per_node)
    rows: list[dict[str, Any]] = []
    idle_by_node: dict[str, int] = {}
    for identity, entry in stats.items():
        avg = entry["avg"]
        node = _series_node(dict(identity), node_names) or "(unmatched)"
        is_idle = avg is not None and avg < threshold
        if is_idle:
            idle_by_node[node] = idle_by_node.get(node, 0) + 1
        rows.append(
            {
                "NODE": node,
                "GPU": _render_gpu_identity(identity),
                "AVG": _render_util(avg),
                "MAX": _render_util(entry["max"]),
                "VERDICT": "idle" if is_idle else "active",
                "_avg": avg if avg is not None else float("inf"),
            }
        )
    rows.sort(key=lambda row: (row["_avg"], row["NODE"], row["GPU"]))
    shown = rows[:limit]

    lines = [f"## Idle GPU resources (avg < {threshold:g} over {duration})", f"Metric: {metric_name}"]
    lines.append(short_table(
        [{k: v for k, v in row.items() if not k.startswith("_")} for row in shown],
        ["NODE", "GPU", "AVG", "MAX", "VERDICT"],
    ))

    findings: list[str] = []
    if idle_by_node:
        rollup = [
            {
                "NODE": node,
                "GPU_UNITS": _format_quantity(per_node.get(node, {}).get("allocatable", 0.0)),
                "HELD": _format_quantity(per_node.get(node, {}).get("held", 0.0)),
                "IDLE_GPUS": str(idle_by_node[node]),
            }
            for node in sorted(idle_by_node, key=lambda n: -idle_by_node[n])
        ]
        lines.append("\n### Idle rollup (per node)")
        lines.append(short_table(rollup, ["NODE", "GPU_UNITS", "HELD", "IDLE_GPUS"]))
        reserved_idle = [row["NODE"] for row in rollup if float(row["HELD"]) > 0]
        if reserved_idle:
            findings.append(f"- **WARN** — Nodes {', '.join(reserved_idle)} hold Pod GPU units while their "
                            "GPUs averaged idle; confirm with the owning workloads before consolidating.")
        else:
            findings.append("- **OK** — Idle GPUs are not held by any Pod; candidates for scale-down.")
    else:
        findings.append(f"- **OK** — No GPU series averaged below {threshold:g} over {duration}.")
    lines.append("\n### Findings")
    lines.extend(findings)
    if len(rows) > len(shown):
        lines.append(f"Truncated at limit={limit}; rerun with a higher limit (max 200).")
    return "\n".join(lines)


def register(mcp) -> None:
    mcp.tool()(gpu_capacity_analyze)
    mcp.tool()(gpu_idle_resources)
