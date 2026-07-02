"""Prometheus integration: PromQL query / query_range / pod_metrics.

Three layers of access to Prometheus:

  - `prometheus_query(promql, time?)`        — instant PromQL query
  - `prometheus_query_range(promql, start, end, step)` — PromQL range query
  - `pod_metrics(pod_name, namespace, metric, range)`  — high-level wrapper
    around common container metrics (CPU / memory / network) using cAdvisor
    metric names that the Prometheus node-exporter / kubelet pipeline
    emits by default.

Endpoint discovery:
  1. `K8S_MCP_PROMETHEUS_URL` env var (highest priority).
  2. Auto-scan a small list of common (namespace, service) pairs — covers
     kube-prometheus-stack, prometheus-operator, and bare Prometheus
     deployments.
  3. If neither works, return a friendly error that asks the user for the
     URL — this is the "再查询的时候增加一次询问" path.

Auth: bearer token via `K8S_MCP_PROMETHEUS_BEARER_TOKEN`. Most local
Prometheus deployments don't need it.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any

from kubernetes import client
from kubernetes.client.rest import ApiException

from ..config import Settings, get_settings
from ..formatters import short_table

logger = logging.getLogger(__name__)


# Common candidate Service names × namespaces where Prometheus typically
# runs. Order = priority. The first Service that exists wins.
_PROM_CANDIDATES: list[tuple[str, str]] = [
    ("monitoring", "kube-prometheus-stack-prometheus"),
    ("monitoring", "prometheus-operated"),
    ("monitoring", "prometheus"),
    ("monitoring", "prometheus-server"),
    ("prometheus", "prometheus"),
    ("prometheus", "prometheus-server"),
    ("prometheus", "prometheus-operated"),
    ("kube-prometheus", "prometheus"),
    ("kube-prometheus", "prometheus-operated"),
    ("observability", "prometheus"),
    ("observability", "prometheus-operated"),
]

_DISCOVERY_CACHE: str | None = None
_DISCOVERY_TRIED: bool = False


# =============================================================================
# Discovery
# =============================================================================


def _resolve_prometheus_url(settings: Settings) -> str:
    """Return a usable Prometheus base URL or raise LookupError.

    Resolution order:
      1. `settings.prometheus_url` (explicit env var)
      2. Auto-scan candidate (namespace, service) pairs in the cluster
      3. Raise LookupError with a helpful "ask the user" message

    The result of (2) is cached for the lifetime of the process.
    """
    global _DISCOVERY_CACHE, _DISCOVERY_TRIED

    if settings.prometheus_url:
        return settings.prometheus_url.rstrip("/")

    if _DISCOVERY_CACHE:
        return _DISCOVERY_CACHE

    if _DISCOVERY_TRIED:
        # We already scanned and found nothing; don't spam the apiserver.
        raise LookupError(_not_found_message())

    _DISCOVERY_TRIED = True

    core = client.CoreV1Api()
    tried: list[str] = []
    for ns, svc in _PROM_CANDIDATES:
        tried.append(f"{ns}/{svc}")
        try:
            core.read_namespaced_service(name=svc, namespace=ns)
        except ApiException as e:
            if e.status == 404:
                continue
            # RBAC denied / transient error → don't crash the whole tool;
            # fall through to LookupError so the agent can react.
            logger.debug("prom discovery: error reading %s/%s: %s", ns, svc, e)
            continue
        except Exception as e:  # noqa: BLE001 — defensive
            logger.debug("prom discovery: unexpected error for %s/%s: %s", ns, svc, e)
            continue

        # Found a candidate. Resolve ClusterIP + port.
        try:
            obj = core.read_namespaced_service(name=svc, namespace=ns)
            url = _service_url(obj)
        except Exception as e:  # noqa: BLE001 — defensive
            logger.debug("prom discovery: cannot build URL from %s/%s: %s", ns, svc, e)
            continue
        _DISCOVERY_CACHE = url
        logger.info("Auto-discovered Prometheus at %s (Service %s/%s)", url, ns, svc)
        return url

    raise LookupError(_not_found_message(tried=tried))


def _service_url(svc_obj: Any) -> str:
    """Build a base URL from a CoreV1Service object: scheme://clusterIP:port."""
    spec = svc_obj.spec
    if not spec or not spec.cluster_ip:
        raise ValueError("service has no ClusterIP")
    scheme = "http"
    port = 9090
    if spec.ports:
        for p in spec.ports:
            if p.name == "http" or p.name == "web":
                port = p.port
                break
        else:
            port = spec.ports[0].port
    return f"{scheme}://{spec.cluster_ip}:{port}"


def _not_found_message(tried: list[str] | None = None) -> str:
    msg = (
        "Prometheus is not auto-discoverable in this cluster.\n"
        "Searched these (namespace, Service) pairs:\n"
    )
    for t in (tried or [f"{ns}/{svc}" for ns, svc in _PROM_CANDIDATES]):
        msg += f"  - {t}\n"
    msg += (
        "\nTo enable Prometheus metrics, do ONE of:\n"
        "  1. Ask the user: 'What is your Prometheus URL?' — then set\n"
        "     `K8S_MCP_PROMETHEUS_URL=http://prometheus.<ns>.svc.cluster.local:9090`\n"
        "     in the MCP server's env block (Claude Desktop / Cursor / etc.).\n"
        "  2. Confirm a Service named `prometheus`, `prometheus-operated`,\n"
        "     `kube-prometheus-stack-prometheus`, or `prometheus-server` exists\n"
        "     in your cluster (use `get_resource(Service, ...)`).\n"
    )
    return msg


def reset_prometheus_discovery_cache() -> None:
    """Drop the cached Prometheus URL. Tests should call this between scenarios."""
    global _DISCOVERY_CACHE, _DISCOVERY_TRIED
    _DISCOVERY_CACHE = None
    _DISCOVERY_TRIED = False


# =============================================================================
# HTTP transport
# =============================================================================


def _prom_get(path: str, params: dict[str, str], base_url: str,
              bearer_token: str | None) -> dict[str, Any]:
    """Hit Prometheus's HTTP API. Returns parsed JSON.

    On HTTP error, raises a ValueError whose message embeds Prometheus's
    own errorType / error fields — much friendlier than a raw urllib error.
    """
    url = f"{base_url}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, method="GET")
    if bearer_token:
        req.add_header("Authorization", f"Bearer {bearer_token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise ValueError(
            f"Prometheus HTTP {e.code} {e.reason} for {url}\n{body}"
        ) from e
    except urllib.error.URLError as e:
        raise ValueError(f"Cannot reach Prometheus at {url}: {e.reason}") from e
    except TimeoutError as e:
        raise ValueError(f"Prometheus at {url} timed out after 15s") from e

    try:
        return json.loads(data)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Prometheus at {url} returned non-JSON ({len(data)} bytes): {e}"
        ) from e


# =============================================================================
# Public tools
# =============================================================================


def prometheus_query(promql: str, time: str | None = None) -> str:
    """Run an instant PromQL query against Prometheus.

    Args:
        promql: the PromQL expression, e.g.
            `rate(container_cpu_usage_seconds_total{pod="web-1"}[5m])`.
        time: optional RFC3339 timestamp for the query. Default = "now".

    Returns:
        A formatted table. Each result row has METRIC (the metric name and
        any non-default labels) and VALUE. Empty result returns a helpful
        "no data points" notice.

    Errors:
      LookupError if Prometheus isn't reachable (auto-discovery failed
      and no `K8S_MCP_PROMETHEUS_URL` is set).
      ValueError if Prometheus returns an error or the network call fails.
    """
    settings = get_settings()
    base = _resolve_prometheus_url(settings)

    params: dict[str, str] = {"query": promql}
    if time:
        params["time"] = time

    payload = _prom_get(
        "/api/v1/query", params, base, settings.prometheus_bearer_token
    )

    if payload.get("status") != "success":
        err = payload.get("error") or "unknown error"
        err_type = payload.get("errorType", "")
        raise ValueError(f"Prometheus query failed ({err_type}): {err}")

    data = payload.get("data") or {}
    result_type = data.get("resultType", "")
    result = data.get("result") or []

    if not result:
        return (
            f"(no data points for `{promql}`. "
            "Possible causes: time range empty, no matching series, "
            "or Prometheus just started up with empty TSDB.)"
        )

    if result_type == "scalar":
        # single value, no labels
        v = result[1] if isinstance(result, list) and len(result) == 2 else result
        return f"{promql} = {v}"

    if result_type == "string":
        return f"{promql} = {result[1] if isinstance(result, list) else result}"

    # vector / matrix both come as [{metric: {...}, value|values: ...}]
    rows: list[dict[str, str]] = []
    for r in result:
        metric = r.get("metric") or {}
        name = metric.pop("__name__", "") if isinstance(metric, dict) else ""
        labels = ",".join(f'{k}="{v}"' for k, v in sorted(metric.items()))
        full = f"{name}{{{labels}}}" if labels else name
        v = r.get("value") or r.get("values")
        if isinstance(v, list) and v and isinstance(v[0], list):
            # matrix result (shouldn't appear here for instant query, but
            # be defensive)
            v_str = "; ".join(f"{ts}={val}" for ts, val in v)
        elif isinstance(v, list) and len(v) == 2:
            ts, val = v
            v_str = f"{val} @ {ts}"
        else:
            v_str = str(v)
        rows.append({"METRIC": full, "VALUE": v_str})
    return short_table(rows, ["METRIC", "VALUE"])


def prometheus_query_range(
    promql: str,
    start: str,
    end: str,
    step: str = "30s",
) -> str:
    """Run a range PromQL query (time series) against Prometheus.

    Args:
        promql: PromQL expression.
        start: RFC3339 start time, e.g. "2026-07-02T14:00:00Z".
        end:   RFC3339 end time.
        step:  query resolution step width (Prometheus duration: "15s", "1m",
            "5m", "1h"). Smaller step → more data points.

    Returns:
        A formatted table per series. Each series has its own block with
        METRIC / TIMESTAMP / VALUE rows. Empty series return a notice.

    See `prometheus_query` for error semantics.
    """
    settings = get_settings()
    base = _resolve_prometheus_url(settings)

    payload = _prom_get(
        "/api/v1/query_range",
        {"query": promql, "start": start, "end": end, "step": step},
        base,
        settings.prometheus_bearer_token,
    )

    if payload.get("status") != "success":
        err = payload.get("error") or "unknown error"
        err_type = payload.get("errorType", "")
        raise ValueError(f"Prometheus range query failed ({err_type}): {err}")

    result = (payload.get("data") or {}).get("result") or []
    if not result:
        return (
            f"(no data points for `{promql}` in [{start} → {end}] step={step}. "
            "Possible causes: window too narrow, no matching series, or "
            "the metric isn't being scraped.)"
        )

    blocks: list[str] = []
    for r in result:
        metric = r.get("metric") or {}
        name = metric.pop("__name__", "") if isinstance(metric, dict) else ""
        labels = ",".join(f'{k}="{v}"' for k, v in sorted(metric.items()))
        full = f"{name}{{{labels}}}" if labels else name
        values = r.get("values") or []
        if not values:
            blocks.append(f"=== {full} ===\n(no points in range)")
            continue
        rows: list[dict[str, str]] = []
        for ts, val in values:
            ts_human = _ts_human(ts)
            rows.append({"TIMESTAMP": ts_human, "VALUE": val})
        blocks.append(f"=== {full} ===\n" + short_table(rows, ["TIMESTAMP", "VALUE"]))
    return "\n\n".join(blocks)


# =============================================================================
# pod_metrics — high-level wrapper for the 90% case
# =============================================================================


_PROMQL_TEMPLATES: dict[str, str] = {
    # `range` will be substituted; `pod` and `namespace` substituted by
    # `_substitute_labels`. `container` filters out the pause container
    # that kubelet injects so the agent sees just real workload containers.
    "cpu": (
        'sum by (container) ('
        'rate(container_cpu_usage_seconds_total'
        '{__labels__}[__range__])'
        ')'
    ),
    "memory": (
        'sum by (container) ('
        'container_memory_working_set_bytes'
        '{__labels__}'
        ')'
    ),
    "network_rx": (
        'sum ('
        'rate(container_network_receive_bytes_total'
        '{__labels__}[__range__])'
        ')'
    ),
    "network_tx": (
        'sum ('
        'rate(container_network_transmit_bytes_total'
        '{__labels__}[__range__])'
        ')'
    ),
    "fs_reads": (
        'sum by (container) ('
        'rate(container_fs_reads_bytes_total'
        '{__labels__}[__range__])'
        ')'
    ),
    "fs_writes": (
        'sum by (container) ('
        'rate(container_fs_writes_bytes_total'
        '{__labels__}[__range__])'
        ')'
    ),
}


def pod_metrics(
    pod_name: str,
    namespace: str,
    metric: str = "cpu",
    range: str = "5m",
) -> str:
    """Fetch a common container metric for a single Pod.

    Args:
        pod_name: pod name. Use a regex prefix (e.g. "nginx-7c5b.*") if you
            want to aggregate across replicas of the same deployment; the
            underlying cAdvisor metrics are emitted per container, not per
            Pod, so summing across multiple Pods is usually what you want.
        namespace: pod's namespace.
        metric: one of:
            - "cpu"        — CPU cores used (5m rate by default)
            - "memory"     — RSS-equivalent memory in bytes (instantaneous)
            - "network_rx" — bytes/sec received across all interfaces
            - "network_tx" — bytes/sec transmitted across all interfaces
            - "fs_reads"   — bytes/sec read from filesystem
            - "fs_writes"  — bytes/sec written to filesystem
        range: rate window for rate-based metrics (Prometheus duration).
            Ignored by "memory" (instantaneous).

    Returns:
        Human-readable summary with one line per container:
            Pod default/nginx-7c5b-abc
              container=app   cpu=0.024 cores
              container=sidecar cpu=0.001 cores

    Note:
        These metrics come from cAdvisor via the kubelet, which Prometheus
        scrapes by default. If your cluster uses a non-standard metric
        name, fall back to `prometheus_query` with a custom PromQL.
    """
    if metric not in _PROMQL_TEMPLATES:
        raise ValueError(
            f"metric={metric!r} not supported. "
            f"Choose one of: {sorted(_PROMQL_TEMPLATES)}. "
            "For custom PromQL use prometheus_query()."
        )

    labels = (
        f'pod=~"{pod_name}", namespace="{namespace}", '
        f'container!="", container!="POD"'
    )
    promql = _PROMQL_TEMPLATES[metric].replace("__labels__", labels).replace(
        "__range__", range
    )

    # Call Prometheus directly rather than going through prometheus_query's
    # formatted table — we want structured data, not a string we'd have to
    # re-parse (fragile: the closing brace can land in the value column).
    settings = get_settings()
    base = _resolve_prometheus_url(settings)
    payload = _prom_get(
        "/api/v1/query",
        {"query": promql},
        base,
        settings.prometheus_bearer_token,
    )

    if payload.get("status") != "success":
        err = payload.get("error") or "unknown error"
        raise ValueError(f"Prometheus query failed ({payload.get('errorType', '')}): {err}")

    result = (payload.get("data") or {}).get("result") or []
    if not result:
        return (
            f"Pod {namespace}/{pod_name}: no Prometheus data for "
            f"metric={metric!r} range={range!r}.\n"
            "Possibilities: the Pod just started, cAdvisor isn't being "
            "scraped, or the metric name differs from the cAdvisor default."
        )

    lines = [f"Pod {namespace}/{pod_name} — metric={metric} range={range}"]
    for r in result:
        m = r.get("metric") or {}
        v = r.get("value") or []
        value = v[1] if isinstance(v, list) and len(v) == 2 else v
        container = m.get("container") or "<total>"
        unit = _unit_for(metric)
        lines.append(f"  container={container}: {value} {unit}".rstrip())
    return "\n".join(lines)


def _unit_for(metric: str) -> str:
    if metric == "cpu":
        return "cores"
    if metric == "memory":
        return "bytes"
    if metric.startswith("network_") or metric.startswith("fs_"):
        return "B/s"
    return ""


def _extract_label(metric_str: str, label: str) -> str | None:
    """Pull one label value out of a `name{key="val",...}` string."""
    if "{" not in metric_str:
        return None
    inside = metric_str[metric_str.index("{") + 1 : metric_str.rindex("}")]
    for part in inside.split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k.strip() == label:
            return v.strip().strip('"')
    return None


def _ts_human(unix_ts: str | float) -> str:
    """Format a Prometheus Unix timestamp as RFC3339-UTC for readability."""
    try:
        return datetime.fromtimestamp(float(unix_ts), tz=UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (ValueError, TypeError, OSError):
        return str(unix_ts)


def register(mcp) -> None:
    mcp.tool()(prometheus_query)
    mcp.tool()(prometheus_query_range)
    mcp.tool()(pod_metrics)
