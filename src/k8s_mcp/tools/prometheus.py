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

import atexit
import json
import logging
import os
import random
import re
import shutil
import signal
import socket
import subprocess
import time
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


# Common Service name patterns we'd consider "likely Prometheus". The fuzzy
# match is intentionally generous so a non-standard install (e.g. someone
# renamed it "monitor-prometheus" or "prom" or "prom-kube") still gets
# surfaced to the agent rather than silently ignored.
_PROM_NAME_HINTS: tuple[str, ...] = (
    "prometheus",
    "kube-prometheus",
    "prom",
)


def find_prometheus_service(namespace: str | None = None) -> str:
    """Discover where Prometheus is running in the cluster.

    Different clusters install Prometheus into different namespaces and
    with different Service names — kube-prometheus-stack typically uses
    `monitoring/kube-prometheus-stack-prometheus`, the operator uses
    `prometheus-operated`, bare manifests use `prometheus`. Rather than
    hard-coding a list (which fails on novel installs), this tool exposes
    a wider scan so the agent can find Prometheus in *any* namespace.

    Args:
        namespace: optional — limit search to a single namespace. If
            omitted, scans every namespace in the cluster (cheap; ns list
            is one API call).

    Returns:
        A formatted table of candidate Services. Each row has
        NAMESPACE, NAME, CLUSTER_IP, PORT, URL. The agent should pick the
        row that looks like Prometheus and pass `url=<URL>` to
        `prometheus_query` / `prometheus_query_range` / `pod_metrics`.

        Empty result returns a "no Prometheus Services found" notice with
        suggestions (install kube-prometheus-stack; or set
        `K8S_MCP_PROMETHEUS_URL`).

    Errors:
        ApiException on cluster-level API errors (RBAC, apiserver down).

    This is the **first step** of the recommended flow:

        find_prometheus_service()  →  pick a URL from the table
        prometheus_query(..., prometheus_url=<that URL>)
    """
    core = client.CoreV1Api()

    if namespace:
        namespaces = [namespace]
        svcs_iter = (
            (s.metadata.namespace, s)
            for s in core.list_namespaced_service(namespace=namespace).items
        )
    else:
        namespaces = [ns.metadata.name for ns in core.list_namespace().items]
        svcs_iter = (
            (s.metadata.namespace, s)
            for ns_name in namespaces
            for s in core.list_namespaced_service(namespace=ns_name).items
        )

    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for ns_name, svc in svcs_iter:
        name = svc.metadata.name
        key = (ns_name, name)
        if key in seen:
            continue
        seen.add(key)

        lname = name.lower()
        if not any(hint in lname for hint in _PROM_NAME_HINTS):
            continue

        try:
            url = _service_url(svc)
        except Exception as e:  # noqa: BLE001 — defensive
            logger.debug("find_prometheus_service: skip %s/%s: %s", ns_name, name, e)
            continue

        port = (svc.spec.ports[0].port if svc.spec and svc.spec.ports else 9090)
        rows.append(
            {
                "NAMESPACE": ns_name,
                "NAME": name,
                "CLUSTER_IP": svc.spec.cluster_ip if svc.spec else "",
                "PORT": str(port),
                "URL": url,
            }
        )

    if not rows:
        scanned = (
            f"namespace(s)={namespace}" if namespace else f"{len(namespaces)} namespaces"
        )
        return (
            f"No Prometheus-looking Services found in {scanned}.\n"
            "Hints:\n"
            "  - Install kube-prometheus-stack, the prometheus-operator, or "
            "a bare Prometheus chart.\n"
            "  - Or set `K8S_MCP_PROMETHEUS_URL` in the MCP server's env "
            "block to skip discovery."
        )

    return short_table(
        rows, ["NAMESPACE", "NAME", "CLUSTER_IP", "PORT", "URL"]
    )


# =============================================================================
# URL resolution (with override support)
# =============================================================================


def _resolve_base(passed_url: str | None, settings: Settings) -> str:
    """Pick the Prometheus base URL, in priority order:

      1. `passed_url` — agent-discovered / user-provided URL passed to the
         tool directly (highest priority; the agent typically discovers via
         `list_resources(Service)` or `find_prometheus_service()` and
         threads it through).
      2. `settings.prometheus_url` — `K8S_MCP_PROMETHEUS_URL` env var.
      3. Auto-scan candidate (namespace, Service) pairs.
      4. Raise LookupError with the "ask the user" message.
    """
    if passed_url:
        return passed_url.rstrip("/")
    return _resolve_prometheus_url(settings)


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


def prometheus_query(
    promql: str,
    time: str | None = None,
    prometheus_url: str | None = None,
) -> str:
    """Run an instant PromQL query against Prometheus.

    Args:
        promql: the PromQL expression, e.g.
            `rate(container_cpu_usage_seconds_total{pod="web-1"}[5m])`.
        time: optional RFC3339 timestamp for the query. Default = "now".
        prometheus_url: optional explicit URL (e.g.
            `http://prometheus.monitoring.svc.cluster.local:9090`).
            If omitted, falls back to `K8S_MCP_PROMETHEUS_URL` env var,
            then auto-discovery. Agents should typically discover the URL
            via `find_prometheus_service(namespace=...)` first, then pass
            it here — this is the "MCP and the LLM collaborate" pattern
            that lets one binary serve clusters with Prometheus in any
            namespace.

    Returns:
        A formatted table. Each result row has METRIC (the metric name and
        any non-default labels) and VALUE. Empty result returns a helpful
        "no data points" notice.

    Errors:
      LookupError if Prometheus isn't reachable (no override, no env var,
      auto-discovery failed).
      ValueError if Prometheus returns an error or the network call fails.
    """
    settings = get_settings()
    base = _resolve_base(prometheus_url, settings)

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
        v = result[1] if isinstance(result, list) and len(result) == 2 else result
        return f"{promql} = {v}"

    if result_type == "string":
        return f"{promql} = {result[1] if isinstance(result, list) else result}"

    rows: list[dict[str, str]] = []
    for r in result:
        metric = r.get("metric") or {}
        name = metric.pop("__name__", "") if isinstance(metric, dict) else ""
        labels = ",".join(f'{k}="{v}"' for k, v in sorted(metric.items()))
        full = f"{name}{{{labels}}}" if labels else name
        v = r.get("value") or r.get("values")
        if isinstance(v, list) and v and isinstance(v[0], list):
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
    prometheus_url: str | None = None,
) -> str:
    """Run a range PromQL query (time series) against Prometheus.

    Args:
        promql: PromQL expression.
        start: RFC3339 start time, e.g. "2026-07-02T14:00:00Z".
        end:   RFC3339 end time.
        step:  query resolution step width (Prometheus duration: "15s", "1m",
            "5m", "1h"). Smaller step → more data points.
        prometheus_url: optional explicit URL — see `prometheus_query` for
            the discovery pattern. Use `find_prometheus_service()` to
            locate Prometheus, then pass the URL here.

    Returns:
        A formatted table per series. Each series has its own block with
        METRIC / TIMESTAMP / VALUE rows. Empty series return a notice.

    See `prometheus_query` for error semantics.
    """
    settings = get_settings()
    base = _resolve_base(prometheus_url, settings)

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
    prometheus_url: str | None = None,
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
        prometheus_url: optional explicit URL — see `prometheus_query` for
            the discovery pattern. Use `find_prometheus_service()` to
            locate Prometheus, then pass the URL here.

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
    base = _resolve_base(prometheus_url, settings)
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


# =============================================================================
# port_forward — bridge ClusterIP Services to the local process
# =============================================================================
#
# Why this exists: Prometheus Services are almost always ClusterIP (default),
# which means a `10.96.x.x` virtual IP only routable from inside the cluster.
# The MCP server runs as a stdio child of Cherry Studio / Claude Desktop —
# i.e. on the user's machine, *outside* the cluster. So hitting
# `http://10.96.3.39:9090/api/v1/query` from this process gets a TCP RST
# (which urllib renders as "Remote end closed connection without response").
#
# `kubectl port-forward` is the standard fix: it speaks to the apiserver over
# SPDY, and the apiserver terminates the TCP connection to the ClusterIP on
# your behalf. We launch it as a managed subprocess so that:
#
#   - The process tree dies with the MCP server (no orphan kubectl procs).
#   - We pick a free local port deterministically and report the URL back.
#   - The Agent can hand the URL straight to `prometheus_query(...,
#     prometheus_url=...)` without any extra plumbing.
# =============================================================================


_PF_REGISTRY: dict[str, dict[str, Any]] = {}
_PF_HEALTHCHECK_TTL_S = 30  # if a managed kubectl hasn't shown life in this
                            # long we assume it's dead and forget it.


# Service / namespace / port inputs are passed to subprocess as argv (no shell),
# so most injection vectors are closed. We still validate heavily — both for
# sanity (don't forward `kube-system/kube-dns` by accident) and to make the
# error messages user-friendly instead of "kubectl invocation failed".
_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?(\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)*$")


def _validate_k8s_name(value: str, kind: str) -> None:
    """Validate a string is a legal RFC 1123 K8s resource name."""
    if not value or not _NAME_RE.match(value):
        raise ValueError(
            f"invalid {kind}={value!r}: must match RFC 1123 label "
            "(lowercase alphanum / '-', '.' allowed for some kinds)"
        )


def _validate_port(value: int, kind: str) -> int:
    """Coerce and validate a TCP port (1..65535)."""
    try:
        port = int(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{kind}={value!r} is not an integer") from e
    if not (1 <= port <= 65535):
        raise ValueError(f"{kind}={port} outside 1..65535")
    return port


def _pick_free_local_port() -> int:
    """Ask the kernel for a TCP port that is currently free.

    Race-condition window between this call and the subprocess binding it is
    closed by `_wait_port_ready()`, which probes until success or timeout.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port_ready(host: str, port: int, timeout_s: float = 10.0) -> None:
    """Block until `host:port` accepts a TCP connection, or `timeout_s` elapses.

    We probe with a fresh socket rather than reuse — `kubectl port-forward`
    binds lazily, and a refused connect is the right "not yet" signal.
    """
    deadline = time.monotonic() + timeout_s
    last_err: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError as e:
            last_err = e
            time.sleep(0.1)
    raise TimeoutError(
        f"port-forward never became ready on {host}:{port} "
        f"after {timeout_s:.1f}s ({last_err})"
    )


def _prune_dead_forwards() -> None:
    """Forget entries whose kubectl subprocess has died."""
    for fid, entry in list(_PF_REGISTRY.items()):
        proc: subprocess.Popen = entry["proc"]
        if proc.poll() is not None:
            logger.info(
                "port_forward: %s gone (exit=%s), removing from registry",
                fid, proc.returncode,
            )
            del _PF_REGISTRY[fid]


def start_prometheus_port_forward(
    namespace: str,
    service_name: str,
    service_port: int = 9090,
    local_port: int | None = None,
) -> str:
    """Bridge an in-cluster Prometheus Service to 127.0.0.1 via port-forward.

    Most Prometheus installs expose the API on a `ClusterIP` Service — a
    virtual IP (typically `10.96.x.x`) that's only routable from inside the
    cluster. The MCP server runs *outside* the cluster, so even with the
    right IP and port, TCP packets get dropped. This tool starts a managed
    `kubectl port-forward` that uses the apiserver's SPDY endpoint to
    terminate the connection for you, and returns the local URL to thread
    into the Prometheus tools.

    Args:
        namespace: where the Prometheus Service lives (use
            `find_prometheus_service()` to find this).
        service_name: the Prometheus Service name.
        service_port: target port on the Service (default `9090`).
        local_port: TCP port on `127.0.0.1` to bind — auto-picked if `None`.

    Returns:
        A friendly status string. Includes:
          - forward_id    — for `stop_port_forward`
          - local URL     — pass this as `prometheus_url=` to the
                            Prometheus query tools
          - pid           — for debugging

    Example flow::

        find_prometheus_service() → pick row "monitoring/prometheus"
        start_prometheus_port_forward("monitoring", "prometheus")
        prometheus_query("up", prometheus_url="http://127.0.0.1:34567")

    Lifecycle: the subprocess is reaped when the MCP server exits (via
    `atexit`). Restarting Cherry Studio's MCP entry therefore kills all
    running forwards — re-call this tool after restart.
    """
    _validate_k8s_name(namespace, "namespace")
    _validate_k8s_name(service_name, "service_name")
    service_port = _validate_port(service_port, "service_port")

    kubectl = shutil.which("kubectl")
    if not kubectl:
        raise RuntimeError(
            "`kubectl` is not on PATH; port-forward requires it. "
            "Install kubectl, or run this MCP server inside the cluster "
            "(in-cluster mode) so the Service is reachable directly."
        )

    if local_port is not None:
        local_port = _validate_port(local_port, "local_port")
    else:
        local_port = _pick_free_local_port()

    # Idempotency: re-forwarding the same target is cheap and common.
    # Match on (ns, service, service_port) so caller can pick the URL blindly.
    _prune_dead_forwards()
    for fid, entry in _PF_REGISTRY.items():
        if (
            entry["namespace"] == namespace
            and entry["service"] == service_name
            and entry["service_port"] == service_port
        ):
            url = entry["url"]
            return (
                f"Forward already active: {fid}\n"
                f"local URL: {url}\n"
                f"Pass this URL as `prometheus_url=` to the Prometheus tools."
            )

    cmd = [
        kubectl, "port-forward",
        "--namespace", namespace,
        f"svc/{service_name}",
        f"{local_port}:{service_port}",
        # Don't block on a TTY — we're a stdio MCP server.
        "--address", "127.0.0.1",
    ]
    logger.info("port_forward: starting: %s", cmd)
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        # New process group so we can SIGTERM everything if it forks.
        start_new_session=True,
    )

    try:
        _wait_port_ready("127.0.0.1", local_port, timeout_s=10.0)
    except Exception:
        # Forward didn't come up — kill the half-started process and surface
        # what kubectl said, so the user knows *why* ("port in use" vs
        # "service not found" vs "RBAC denied").
        try:
            proc.terminate()
            try:
                err = proc.stderr.read(4096).decode("utf-8", errors="replace")
            except Exception:
                err = ""
        finally:
            proc.wait(timeout=2)
        raise RuntimeError(
            f"kubectl port-forward for {namespace}/{service_name}:{service_port} "
            f"→ 127.0.0.1:{local_port} failed to come up.\n"
            f"kubectl stderr:\n{err.strip() or '(empty)'}"
        ) from None

    forward_id = (
        f"{namespace}/{service_name}:{service_port}"
        f"→127.0.0.1:{local_port}"
    )
    url = f"http://127.0.0.1:{local_port}"
    _PF_REGISTRY[forward_id] = {
        "proc": proc,
        "namespace": namespace,
        "service": service_name,
        "service_port": service_port,
        "local_port": local_port,
        "url": url,
        "started_at": time.time(),
        "pid": proc.pid,
    }
    logger.info("port_forward: ready %s pid=%d", forward_id, proc.pid)
    return (
        f"Forward started: {forward_id}\n"
        f"local URL: {url}\n"
        f"pid: {proc.pid}\n"
        f"Pass `{url}` as `prometheus_url=` to the Prometheus query tools.\n"
        f"Stop with `stop_port_forward(forward_id={forward_id!r})`."
    )


def list_port_forwards() -> str:
    """List currently active port-forwards started by this MCP server.

    Returns:
        A formatted table (FORWARD_ID / URL / PID / AGE). Includes a
        'use stop_port_forward(forward_id=...) to terminate' reminder.

        If no forwards are active, returns a short 'no active forwards'
        notice (not an empty string).
    """
    _prune_dead_forwards()
    if not _PF_REGISTRY:
        return (
            "No active port-forwards.\n"
            "Call `start_prometheus_port_forward(namespace, service_name)` "
            "after `find_prometheus_service()` if you need to reach a "
            "ClusterIP Prometheus from outside the cluster."
        )

    rows: list[dict[str, str]] = []
    now = time.time()
    for fid, entry in _PF_REGISTRY.items():
        age_s = int(now - entry["started_at"])
        age_str = (
            f"{age_s}s" if age_s < 60
            else f"{age_s // 60}m{age_s % 60:02d}s"
        )
        rows.append({
            "FORWARD_ID": fid,
            "URL": entry["url"],
            "PID": str(entry["pid"]),
            "AGE": age_str,
        })
    return short_table(rows, ["FORWARD_ID", "URL", "PID", "AGE"])


def stop_port_forward(forward_id: str) -> str:
    """Terminate a port-forward started by `start_prometheus_port_forward`.

    Args:
        forward_id: the identifier returned by `start_prometheus_port_forward`
            (looks like "default/prometheus:9090→127.0.0.1:34567").

    Returns:
        A short status string. If the forward was already gone (e.g. the
        subprocess died on its own) we still report success — the goal state
        "no forward" is reached either way.
    """
    entry = _PF_REGISTRY.get(forward_id)
    if entry is None:
        # Was it ever there? Tell the user either way.
        return (
            f"No port-forward with id {forward_id!r}.\n"
            "Call `list_port_forwards()` to see active forwards."
        )

    proc: subprocess.Popen = entry["proc"]
    if proc.poll() is None:
        # Try a clean shutdown first; escalate after 2s.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=2.0)

    _PF_REGISTRY.pop(forward_id, None)
    return f"Stopped: {forward_id}"


@atexit.register
def _stop_all_port_forwards_on_exit() -> None:
    """Clean up kubectl subprocesses when the MCP server exits.

    Cherry Studio / Claude Desktop restart the MCP server often; leaving
    orphan port-forwards around would slow every restart (each becomes a
    defunct procs that hold TCP ports briefly). Best-effort: SIGTERM, then
    SIGKILL after a short grace, then move on even if it didn't die.
    """
    for fid, entry in list(_PF_REGISTRY.items()):
        proc: subprocess.Popen = entry["proc"]
        if proc.poll() is not None:
            continue
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except Exception as e:  # noqa: BLE001 — best effort at exit
            logger.debug("atexit: SIGTERM failed for %s: %s", fid, e)
    # Second pass: reap anything still alive after ~1s.
    deadline = time.monotonic() + 1.0
    for _fid, entry in list(_PF_REGISTRY.items()):
        proc: subprocess.Popen = entry["proc"]
        remaining = max(0.0, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:  # noqa: BLE001 — last-ditch; nothing useful to do
                pass


# =============================================================================
# NodePort bridge — externally-reachable clone of a ClusterIP Service
# =============================================================================
#
# Why: ClusterIP Services (`10.96.x.x`) are unroutable from outside the
# cluster. We *could* lean on `kubectl port-forward` for everything, but
# that needs `kubectl` on PATH and a long-lived subprocess — fragile.
#
# A NodePort Service is a K8s primitive: kube-proxy binds the NodePort on
# every Node, traffic reaches the same backing Pods via the existing
# selector. If the cluster's Node IPs are network-reachable from the MCP
# client (typical for VPCs / private networks / corporate clusters), the
# Prometheus tools can hit `http://<node-ip>:<node_port>` directly — no
# extra processes, no extra binaries, and the URL works for every other
# tool the user has too.
#
# This tool creates a *parallel* Service of type=NodePort that points at
# the same Pods as the original ClusterIP Service. The original is left
# alone — in-cluster consumers keep using it, just like before.
# =============================================================================


# Random selection here is a deliberate alternative to K8s's built-in
# auto-assignment: the apiserver picks deterministically when nodePort is
# omitted, which can lead to "I tried twice in this cluster and got the
# same port" confusion. Randomizing locally gives us independent picks
# across calls and makes idempotent retries safer.
_NODEPORT_MIN = 30000
_NODEPORT_MAX = 32767
_NODEPORT_RETRY_LIMIT = 10


def _pick_nodeport_free(core: client.CoreV1Api, namespace: str) -> int:
    """Pick a NodePort (30000-32767) not already in use anywhere on the cluster.

    Rather than relying on the apiserver's own auto-pick (which is
    deterministic per-namespace and can collide across rapid retries), we
    scan current Service nodePorts cluster-wide and pick a non-conflict.
    """
    used: set[int] = set()
    for ns in [s.metadata.name for s in core.list_namespace().items]:
        for svc in core.list_namespaced_service(namespace=ns).items:
            if svc.spec and svc.spec.ports:
                for p in svc.spec.ports:
                    if p.node_port:
                        used.add(p.node_port)
    while True:
        candidate = random.randint(_NODEPORT_MIN, _NODEPORT_MAX)
        if candidate not in used:
            return candidate


def _ensure_prometheus_write_allowed(namespace: str) -> None:
    """Mirror the safety guards other write tools perform.

    Reuses `Settings.ns_allowed` (which folds read-only + namespace
    allowlist into one call) so the invariants stay aligned with the rest
    of the codebase. Cluster-scoped writes (no namespace) would have been
    rejected by the allowlist check anyway.
    """
    settings = get_settings()
    if settings.read_only:
        raise PermissionError(
            "Server is in read-only mode (K8S_MCP_READ_ONLY=true); "
            "creating a NodePort Service requires write access."
        )
    if not settings.ns_allowed(namespace):
        raise PermissionError(
            f"Namespace '{namespace}' is not in K8S_MCP_NAMESPACE_ALLOWLIST; "
            "creating a NodePort Service there is refused."
        )


def _service_selector_matches(orig: client.V1Service, candidate: client.V1Service) -> bool:
    """Return True iff candidate points at exactly the same Pods as orig.

    Selector dict equality is sufficient for K8s label selectors (it's
    just equality-based matching, no set operators). Same-selector
    means same endpoints.
    """
    orig_sel = (orig.spec.selector or {}) if orig.spec else {}
    cand_sel = (candidate.spec.selector or {}) if candidate.spec else {}
    return orig_sel == cand_sel


def _port_spec_for_nodeport(orig_ports: list[client.V1ServicePort] | None) -> list[client.V1ServicePort]:
    """Build V1ServicePort list for the NodePort clone.

    nodePort is omitted — let the apiserver pick from the random range we
    channelled via `_pick_nodeport_free`. All other port fields are
    copied verbatim.
    """
    out: list[client.V1ServicePort] = []
    for p in orig_ports or []:
        out.append(
            client.V1ServicePort(
                name=p.name,
                port=p.port,
                target_port=p.target_port,
                protocol=p.protocol,
                # Deliberately no `node_port` — we set it ourselves below
                # after `_pick_nodeport_free`.
            )
        )
    return out


def expose_prometheus_as_nodeport(
    namespace: str,
    service_name: str,
    name_suffix: str = "-np",
    max_attempts: int = _NODEPORT_RETRY_LIMIT,
) -> str:
    """Create a parallel NodePort Service so the MCP client can reach it directly.

    Most Prometheus installs expose the API on a `ClusterIP` Service —
    a virtual IP only routable from inside the cluster. This tool
    creates a *parallel* `NodePort` Service with the **same selector and
    ports** as the original, so the backing Pods stay accessible in
    two ways (in-cluster via the original ClusterIP; externally via the
    new NodePort). The original Service is never modified — in-cluster
    consumers keep working unchanged.

    If the original Service is already `NodePort` / `LoadBalancer` /
    `ExternalName`, this tool short-circuits and just returns the existing
    URL — no creation needed.

    Args:
        namespace: where the Prometheus Service lives.
        service_name: the existing Service name (use
            `find_prometheus_service()` to find this).
        name_suffix: suffix for the new Service name. Default `-np`.
            Pass `""` to overwrite an in-place conversion (rare; you
            usually want the original left alone, so keep the suffix).
        max_attempts: how many nodePort-pick retries before giving up.

    Returns:
        A formatted summary table (SOURCE / TARGET / TYPE / NODE_PORT /
        URL), plus:

          - the chosen URL template `http://<node-ip>:<node_port>`
            (the agent must substitute a real Node IP — fetch via
            `list_resources(kind=Node)` or `get_resource_jsonpath`)
          - the new Service name (so the agent can clean up via
            `delete_resource(kind=Service, name=<new>)`)

    Errors:
        PermissionError if read-only or namespace allowlist denies writes.
        ValueError on invalid inputs (missing selector, no ports, headless
        service).
        ApiException on cluster-level failures (RBAC, quota, etc.).

    Clean-up: delete the new Service with
    `delete_resource(kind="Service", name=<new>)`. The original is
    untouched.
    """
    _validate_k8s_name(namespace, "namespace")
    _validate_k8s_name(service_name, "service_name")
    if not name_suffix:
        raise ValueError(
            "name_suffix must not be empty — refusing to overwrite the "
            "original Service in place. Pass a suffix like '-np'."
        )

    # Write-side guard. Throws PermissionError on read-only or
    # namespace-allowlist failures, matching every other write tool.
    _ensure_prometheus_write_allowed(namespace)

    core = client.CoreV1Api()

    # 1. Read the original Service (the existing ClusterIP one).
    try:
        orig = core.read_namespaced_service(name=service_name, namespace=namespace)
    except ApiException as e:
        if e.status == 404:
            raise ValueError(
                f"Service {namespace}/{service_name} not found. "
                "Run find_prometheus_service() first to confirm the name."
            ) from e
        raise

    if not orig.spec:
        raise ValueError(f"Service {namespace}/{service_name} has no spec.")

    orig_type = orig.spec.type or "ClusterIP"
    orig_ports = orig.spec.ports or []
    orig_selector = orig.spec.selector or {}

    # 2. Short-circuit: if the original is already externally reachable,
    #    return its URL instead of creating anything.
    if orig_type in {"NodePort", "LoadBalancer", "ExternalName"}:
        node_port = None
        for p in orig_ports:
            if p.node_port:
                node_port = p.node_port
                break
        url_hint = _service_url(orig)
        rows = [{
            "NAMESPACE": namespace,
            "NAME": service_name,
            "TYPE": orig_type,
            "NODE_PORT": str(node_port) if node_port else "(managed)",
            "URL": url_hint,
        }]
        return (
            f"Service {namespace}/{service_name} is already type={orig_type}; "
            "no new Service was created.\n"
            + short_table(rows, ["NAMESPACE", "NAME", "TYPE", "NODE_PORT", "URL"])
            + "\nUse this URL as `prometheus_url=` — if it still fails, the cluster "
            "Node IPs are unreachable from this process; fall back to "
            "`start_prometheus_port_forward()`."
        )

    # 3. ClusterIP path: refuse Headless services (no selector → no Pods).
    if orig.spec.cluster_ip in (None, "None", ""):
        raise ValueError(
            f"Service {namespace}/{service_name} is Headless (no ClusterIP); "
            "can't clone it as NodePort — Prometheus needs a stable virtual IP."
        )
    if not orig_selector:
        raise ValueError(
            f"Service {namespace}/{service_name} has no selector. A NodePort "
            "clone would have nothing to forward to."
        )
    if not orig_ports:
        raise ValueError(
            f"Service {namespace}/{service_name} has no ports. Nothing to clone."
        )

    # 4. Idempotency: if a previous call already created the clone and
    #    its selector still matches, reuse instead of duplicating.
    new_name = f"{service_name}{name_suffix}"
    try:
        existing = core.read_namespaced_service(name=new_name, namespace=namespace)
    except ApiException as e:
        if e.status != 404:
            raise
        existing = None

    if existing and existing.spec and _service_selector_matches(orig, existing):
        # Same Pods — return the existing URL.
        node_port = None
        for p in existing.spec.ports or []:
            if p.node_port:
                node_port = p.node_port
                break
        if node_port is None:
            # Rare: someone changed type back. Force a re-create by deleting.
            existing = None
        else:
            rows = [
                {
                    "NAMESPACE": namespace,
                    "SOURCE": f"{service_name} (kept)",
                    "TARGET": f"{new_name} (existing)",
                    "TYPE": existing.spec.type,
                    "NODE_PORT": str(node_port),
                    "URL": f"http://<node-ip>:{node_port}",
                }
            ]
            return (
                f"NodePort clone already exists: {namespace}/{new_name} "
                f"(type={existing.spec.type}, nodePort={node_port}).\n"
                + short_table(rows, ["NAMESPACE", "SOURCE", "TARGET",
                                     "TYPE", "NODE_PORT", "URL"])
                + "\nNo new Service was created. Use the URL above with "
                "`prometheus_url=`, or delete the clone with "
                f"`delete_resource(kind='Service', name={new_name!r})` "
                "and re-call to get a fresh port."
            )

    # 5. Create the new NodePort Service. Pick a port that's free
    #    cluster-wide; if the apiserver still rejects (race), retry a
    #    few times before giving up.
    body = client.V1Service(
        metadata=client.V1ObjectMeta(
            name=new_name,
            namespace=namespace,
            labels={
                **(orig.metadata.labels or {}),
                "app.kubernetes.io/managed-by": "k8s-mcp",
                "k8s-mcp/based-on": service_name,
            },
            annotations={
                "k8s-mcp/created-by": "expose_prometheus_as_nodeport",
            },
        ),
        spec=client.V1ServiceSpec(
            type="NodePort",
            selector=orig_selector,
            ports=_port_spec_for_nodeport(orig_ports),
        ),
    )

    last_err: ApiException | None = None
    for attempt in range(max_attempts):
        # Re-pick on every attempt so retries don't keep choosing a port
        # the apiserver already told us is taken.
        chosen_port = _pick_nodeport_free(core, namespace)
        assert body.spec is not None
        body.spec.ports = [
            client.V1ServicePort(
                name=p.name,
                port=p.port,
                target_port=p.target_port,
                protocol=p.protocol,
                node_port=chosen_port,
            )
            for p in (body.spec.ports or [])
        ]
        try:
            created = core.create_namespaced_service(
                namespace=namespace, body=body
            )
            break
        except ApiException as e:
            last_err = e
            logger.debug("NodePort %d attempt %d failed: %s", chosen_port, attempt, e)
            continue
    else:
        # Exhausted retries.
        raise RuntimeError(
            f"Failed to create NodePort Service {namespace}/{new_name} after "
            f"{max_attempts} attempts (last error: {last_err})"
        ) from last_err

    # 6. Return a useful summary. URL is a template — the agent needs to
    #    pick a Node IP via `list_resources(kind=Node)`.
    actual_port = None
    if created.spec and created.spec.ports:
        for p in created.spec.ports:
            if p.node_port:
                actual_port = p.node_port
                break

    rows = [
        {
            "NAMESPACE": namespace,
            "SOURCE": f"{service_name} (kept — ClusterIP)",
            "TARGET": f"{new_name} (NodePort)",
            "TYPE": "NodePort",
            "NODE_PORT": str(actual_port) if actual_port else "?",
            "URL": f"http://<node-ip>:{actual_port}" if actual_port else "?",
        },
    ]
    return (
        f"Created NodePort clone: {namespace}/{new_name}\n"
        "Original Service (ClusterIP) is unchanged — in-cluster traffic still flows there.\n"
        + short_table(rows, ["NAMESPACE", "SOURCE", "TARGET",
                             "TYPE", "NODE_PORT", "URL"])
        + "\nNext steps:\n"
        f"  1. Get a Node IP: list_resources(kind='Node') or "
        f"get_resource_jsonpath(kind='Node', path='items[*].status.addresses[?(@.type==\"InternalIP\")].address').\n"
        f"  2. Then: prometheus_query(<your PromQL>, "
        f"prometheus_url='http://<node-ip>:{actual_port}').\n"
        f"To tear down: delete_resource(kind='Service', name={new_name!r})."
    )


def register(mcp) -> None:
    mcp.tool()(prometheus_query)
    mcp.tool()(prometheus_query_range)
    mcp.tool()(pod_metrics)
    mcp.tool()(find_prometheus_service)
    mcp.tool()(start_prometheus_port_forward)
    mcp.tool()(list_port_forwards)
    mcp.tool()(stop_port_forward)
    mcp.tool()(expose_prometheus_as_nodeport)
