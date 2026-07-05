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
import re
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
      2. Auto-scan hardcoded (namespace, service) candidates — covers
         ~80% of standard installs in one apiserver call.
      3. **Wide-scan fallback** — when step 2 misses, scan every namespace
         (bounded by `prometheus_namespace_allowlist` if set) for any
         Service whose name matches the prometheus hints. Catches
         non-standard installs like `default/monitor-kube-prometheus-st-prometheus`.
      4. Raise LookupError with a helpful "ask the user" message.

    The result of (2)/(3) is cached for the lifetime of the process.
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

    # Step 2: hardcoded small candidate list.
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

    # Step 3: wide-scan fallback. Hardcoded list didn't hit, so check
    # every namespace (or every namespace in the allowlist) for Services
    # whose name looks like Prometheus. This catches non-standard installs
    # that put Prometheus in `default/` or other unusual namespaces.
    try:
        matches = _wide_scan_prometheus_matches(core, settings)
    except Exception as e:  # noqa: BLE001 — defensive
        logger.debug("prom discovery: wide-scan fallback failed: %s", e)
        matches = []

    if matches:
        ns_name, svc_obj = matches[0]
        try:
            url = _service_url(svc_obj)
        except Exception as e:  # noqa: BLE001 — defensive
            logger.debug("prom discovery: cannot build URL from %s/%s: %s", ns_name, svc_obj.metadata.name, e)
        else:
            _DISCOVERY_CACHE = url
            logger.info(
                "Auto-discovered Prometheus via wide scan at %s (Service %s/%s)",
                url, ns_name, svc_obj.metadata.name,
            )
            return url

    raise LookupError(_not_found_message(tried=tried))


# Service-name hint tuple is `_PROM_NAME_HINTS` (defined further below);
# referenced lazily so order in the file doesn't matter.


def _wide_scan_prometheus_matches(
    core: client.CoreV1Api,
    settings: Settings,
) -> list[tuple[str, Any]]:
    """Scan every namespace (or allowlist subset) for Prometheus-looking
    Services. Returns a list of `(namespace, svc_obj)` tuples sorted by
    preference: NodePort / LoadBalancer first (externally reachable from
    the MCP client), ClusterIP last. Empty list = nothing found.

    Used by:
      - `_resolve_prometheus_url` as a fallback after hardcoded candidates miss
      - `find_prometheus_service` for cluster-wide discovery

    Bounded by `settings.prometheus_namespace_allowlist` when set — see
    `K8S_MCP_PROMETHEUS_NAMESPACE_ALLOWLIST`. None = scan every namespace.
    """
    allowlist = settings.prometheus_namespace_allowlist
    namespaces = [ns.metadata.name for ns in core.list_namespace().items]
    if allowlist is not None:
        # Allowlist set → only scan these namespaces. Unset (None) = all.
        namespaces = [ns for ns in namespaces if ns in allowlist]
        if not namespaces:
            logger.debug(
                "prom wide-scan: allowlist %s excludes every namespace; skipping",
                allowlist,
            )
            return []

    nodeport_first: list[tuple[str, Any]] = []
    clusterip_after: list[tuple[str, Any]] = []
    for ns_name in namespaces:
        try:
            services = core.list_namespaced_service(namespace=ns_name).items
        except ApiException as e:
            logger.debug("prom wide-scan: list svc in %s failed: %s", ns_name, e)
            continue
        except Exception as e:  # noqa: BLE001 — defensive
            logger.debug("prom wide-scan: unexpected error in %s: %s", ns_name, e)
            continue
        for svc in services:
            name = (svc.metadata.name or "").lower()
            if not any(hint in name for hint in _PROM_NAME_HINTS):
                continue
            spec = svc.spec
            svc_type = (getattr(spec, "type", None) if spec else None) or "ClusterIP"
            # Preference order: NodePort / LoadBalancer first (externally
            # reachable), ClusterIP last.
            if svc_type in ("NodePort", "LoadBalancer"):
                nodeport_first.append((ns_name, svc))
            else:
                clusterip_after.append((ns_name, svc))
    return nodeport_first + clusterip_after


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


def _external_url_and_node_port(svc_obj: Any) -> tuple[str | None, int | None]:
    """For an externally-reachable Service, return (url_template, node_port).

    `url_template` substitutes `<node-ip>` or `<lb-ip>` — the caller has to
    look up the real address. `node_port` is the apiserver-allocated
    nodePort (None for LoadBalancer / non-NodePort types). Returns
    `(None, None)` for ClusterIP / ExternalName / headless / port-less
    services.

    The bug fix: previously the URL field was filled with `port` (the
    cluster-internal port, e.g. 9090) instead of `node_port` (45149),
    which made the URL column misleading for NodePort rows.
    """
    spec = svc_obj.spec if hasattr(svc_obj, "spec") else None
    if not spec or not spec.ports:
        return None, None
    svc_type = getattr(spec, "type", None) or "ClusterIP"
    if svc_type == "NodePort":
        for p in spec.ports:
            if p.node_port:
                return f"http://<node-ip>:{p.node_port}", p.node_port
        return None, None
    if svc_type == "LoadBalancer":
        port = spec.ports[0].port
        return f"http://<lb-ip>:{port}", None
    return None, None


def _not_found_message(tried: list[str] | None = None) -> str:
    msg = (
        "Prometheus is not auto-discoverable in this cluster.\n"
        "Searched these (namespace, Service) pairs:\n"
    )
    for t in (tried or [f"{ns}/{svc}" for ns, svc in _PROM_CANDIDATES]):
        msg += f"  - {t}\n"
    msg += (
        "\nA cluster-wide fallback scan (looking for any Service whose "
        "name contains `prometheus` / `kube-prometheus` / `prom`) also "
        "found nothing. If Prometheus is in a namespace excluded by "
        "`K8S_MCP_PROMETHEUS_NAMESPACE_ALLOWLIST`, widen it.\n"
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
    """Drop the cached Prometheus URL + diagnostic hints. Tests should call
    this between scenarios so cached `(name, base_url) → hint` mappings
    don't leak across fake-apiserver setups."""
    global _DISCOVERY_CACHE, _DISCOVERY_TRIED
    _DISCOVERY_CACHE = None
    _DISCOVERY_TRIED = False
    _DIAGNOSE_CACHE.clear()


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
        **NAMESPACE, NAME, TYPE, RECOMMENDED, URL**. The **RECOMMENDED**
        column tells the agent exactly what to call next:

          - NodePort / LoadBalancer → already externally reachable;
            pass `prometheus_url=<URL>` to `prometheus_query` after
            substituting a real Node / LB IP (fetch via
            `list_resources(kind="Node")` or
            `get_resource_jsonpath(..., path="items[*].status.addresses...")`).
          - ClusterIP → call
            `expose_prometheus_as_nodeport(namespace=<ns>, service_name=<name>)`
            on this row. **This is the recommended path** — the apiserver
            auto-allocates the nodePort, so no TOCTOU race. The original
            Service (and all in-cluster consumers) stay untouched.

        Empty result returns a "no Prometheus-looking Services found"
        notice with suggestions (install kube-prometheus-stack; or set
        `K8S_MCP_PROMETHEUS_URL`).

    Errors:
        ApiException on cluster-level API errors (RBAC, apiserver down).

    This is the **first step** of the recommended flow::

        find_prometheus_service()  →  read the RECOMMENDED column
        expose_prometheus_as_nodeport(<ns>, <name>)     # if ClusterIP
        list_resources(kind="Node")                     # get a Node IP
        prometheus_query(..., prometheus_url='http://<node-ip>:<nodePort>')
    """
    core = client.CoreV1Api()

    if namespace:
        # Explicit single-ns path: allowlist does NOT apply (caller knows
        # what they want; honoring allowlist here would silently drop the
        # only result the agent is asking for).
        pairs = [
            (s.metadata.namespace, s)
            for s in core.list_namespaced_service(namespace=namespace).items
        ]
        scanned_label = f"namespace={namespace}"
    else:
        # Cluster-wide scan via shared helper. Honors
        # `prometheus_namespace_allowlist` when set, so on multi-tenant
        # clusters you can cap the surface (and cost) without losing the
        # non-standard-install detection this tool is for.
        settings = get_settings()
        pairs = _wide_scan_prometheus_matches(core, settings)
        scanned_label = (
            f"{len(pairs)} match(es) across namespaces"
            if settings.prometheus_namespace_allowlist is None
            else f"{len(pairs)} match(es) within allowlist={settings.prometheus_namespace_allowlist}"
        )

    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for ns_name, svc in pairs:
        name = svc.metadata.name
        key = (ns_name, name)
        if key in seen:
            continue
        seen.add(key)

        lname = name.lower()
        if not any(hint in lname for hint in _PROM_NAME_HINTS):
            continue

        svc_type = (
            svc.spec.type if svc.spec and getattr(svc.spec, "type", None)
            else "ClusterIP"
        )
        try:
            url = _service_url(svc)
        except Exception as e:  # noqa: BLE001 — defensive
            logger.debug("find_prometheus_service: skip %s/%s: %s", ns_name, name, e)
            continue
        ext_url, node_port = _external_url_and_node_port(svc)

        if svc_type == "ClusterIP":
            recommended = (
                f"expose_prometheus_as_nodeport("
                f"namespace='{ns_name}', service_name='{name}')"
            )
            url_cell = f"{url} (cluster-internal — NOT reachable from MCP client)"
        elif svc_type == "NodePort":
            recommended = (
                "✅ direct (substitute <node-ip> from list_resources(kind=Node))"
            )
            url_cell = ext_url or url
        elif svc_type == "LoadBalancer":
            recommended = (
                "✅ direct (substitute LB ingress from Service.status)"
            )
            url_cell = ext_url or url
        else:
            # ExternalName / unknown — leave to the agent.
            recommended = (
                f"unsupported type={svc_type}; "
                "ask user for URL or set K8S_MCP_PROMETHEUS_URL"
            )
            url_cell = url

        rows.append(
            {
                "NAMESPACE": ns_name,
                "NAME": name,
                "TYPE": svc_type,
                "NODE_PORT": str(node_port) if node_port else "",
                "RECOMMENDED": recommended,
                "URL": url_cell,
            }
        )

    if not rows:
        return (
            f"No Prometheus-looking Services found ({scanned_label}).\n"
            "Hints:\n"
            "  - Install kube-prometheus-stack, the prometheus-operator, or "
            "a bare Prometheus chart.\n"
            "  - Or set `K8S_MCP_PROMETHEUS_URL` in the MCP server's env "
            "block to skip discovery."
        )

    table = short_table(
        rows, ["NAMESPACE", "NAME", "TYPE", "NODE_PORT", "RECOMMENDED", "URL"]
    )
    guidance = (
        "\nGuidance:\n"
        "  - TYPE=NodePort / LoadBalancer → pass the URL above to "
        "`prometheus_query(..., prometheus_url=...)` after substituting "
        "a real Node / LB IP.\n"
        "  - TYPE=ClusterIP → the URL is NOT reachable from this MCP "
        "client. **RECOMMENDED:** call "
        "`expose_prometheus_as_nodeport(namespace, service_name)` on the "
        "matching row. The apiserver allocates the nodePort atomically "
        "(no race), the original ClusterIP Service is left untouched, and "
        "no `kubectl` binary is needed.\n"
    )
    return table + guidance


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
    # Timeouts: a half-dead Prometheus should fail fast. 3s connect (TCP
    # handshake / TLS) is enough for healthy in-cluster Prometheus; 15s
    # read covers full range queries on big clusters (1d / 7d ranges can
    # easily be 5–10s even when things work). Both shorter than the
    # default infinite urllib would otherwise allow.
    try:
        with urllib.request.urlopen(req, timeout=_PROM_HTTP_TIMEOUT) as resp:
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


# Empty-result diagnostic: cAdvisor / kubelet metrics are scraped by
# multiple jobs; only `job="kubelet"` reliably carries `pod`/`namespace`
# labels. When a cAdvisor query comes back empty, probe the scrape jobs
# once so the agent can fix the matcher in one call instead of
# guess-and-iterate.
_CADVISOR_METRIC_PREFIXES: tuple[str, ...] = ("container_", "pod_", "kube_pod_")


def _extract_promql_metric_name(promql: str) -> str:
    """Return the bare metric name, anchoring on `name{` or `name[`."""
    m = re.search(r"([a-zA-Z_:][a-zA-Z0-9_:]*)(?=[\{\[])", promql)
    return m.group(1) if m else ""


def _diagnose_empty_promql(
    promql: str,
    base_url: str,
    bearer_token: str | None,
) -> str:
    """For empty cAdvisor queries, return a hint listing scrape jobs and
    suggesting `job="kubelet"`. Empty string otherwise (or on probe
    failure — diagnostic must never shadow the main error).

    The scrape-job probe is cached per `(metric_name, base_url)` for
    `_DIAGNOSE_TTL_SECONDS` because dashboards and health checks hammer
    the same empty metric every poll cycle — without a cache we'd add
    one full Prometheus HTTP round-trip per call.
    """
    name = _extract_promql_metric_name(promql)
    if not name or not name.startswith(_CADVISOR_METRIC_PREFIXES):
        return ""

    cache_key = (name, base_url)
    cached = _DIAGNOSE_CACHE.get(cache_key)
    now = time.monotonic()
    if cached is not None and (now - cached[0]) < _DIAGNOSE_TTL_SECONDS:
        return cached[1]

    try:
        payload = _prom_get(
            "/api/v1/query",
            {"query": f"group by (job) ({name})"},
            base_url, bearer_token,
        )
    except Exception as e:  # noqa: BLE001 — diagnostic must never shadow main error
        logger.debug("prometheus empty-result diagnostic failed: %s", e)
        # Negative-cache for a shorter window so we don't keep retrying a
        # broken endpoint on every probe.
        _DIAGNOSE_CACHE[cache_key] = (now, "")
        return ""
    jobs = (payload.get("data") or {}).get("result") or []
    if not jobs:
        _DIAGNOSE_CACHE[cache_key] = (now, "")
        return ""
    rows: list[str] = []
    for r in jobs:
        m = r.get("metric") or {}
        job = m.get("job") or "<unknown>"
        v = r.get("value")
        cnt = v[1] if isinstance(v, list) and len(v) == 2 else "?"
        rows.append(f"`{job}` ({cnt} series)")
    out = (
        "\n\nDiagnostic: `" + name + "` is scraped by "
        + ", ".join(rows) + "."
        "\nOnly `job=\"kubelet\"` carries `pod`/`namespace` labels — "
        "the others are embedded cAdvisor (no pod labels) or cgroup "
        "probes. Re-issue with `job=\"kubelet\"`, e.g. "
        f"`{name}{{job=\"kubelet\", pod=\"...\", namespace=\"...\"}}`."
    )
    _DIAGNOSE_CACHE[cache_key] = (now, out)
    return out


# Cache for `_diagnose_empty_promql`. Keyed by (metric_name, base_url);
# value is `(fetched_at_monotonic, hint_string)`. TTL bounds repeated
# dashboard-style probes from hammering Prometheus.
_DIAGNOSE_CACHE: dict[tuple[str, str], tuple[float, str]] = {}
_DIAGNOSE_TTL_SECONDS = 300  # 5 minutes — matches typical scrape config


# HTTP timeout (connect + read combined) for Prometheus API calls.
# urllib's `timeout` param is a single value applied to both phases; we
# picked 15s as a compromise that:
#   - gives in-cluster Prometheus 3s+ to establish the TCP/TLS handshake
#     and start streaming a response;
#   - leaves ~12s for big range queries (1d / 7d windows over thousands
#     of series can take 5–10s even on a healthy Prom);
#   - caps wall-clock hang time on a dead Prom so the agent's tool call
#     can return a clear error instead of waiting indefinitely.
_PROM_HTTP_TIMEOUT = 15


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
            For cAdvisor / kubelet metrics, include `job="kubelet"` when
            grouping by `pod`/`namespace` — only the kubelet scrape
            carries those labels. Empty cAdvisor queries auto-return a
            hint listing which jobs scrape the metric.
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
        diag = _diagnose_empty_promql(promql, base, settings.prometheus_bearer_token)
        return (
            f"(no data points for `{promql}`. "
            "Possible causes: time range empty, no matching series, "
            "or Prometheus just started up with empty TSDB.)"
            + diag
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
        diag = _diagnose_empty_promql(promql, base, settings.prometheus_bearer_token)
        return (
            f"(no data points for `{promql}` in [{start} → {end}] step={step}. "
            "Possible causes: window too narrow, no matching series, or "
            "the metric isn't being scraped.)"
            + diag
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
    """Fetch a common container metric for one Pod from Prometheus — pick THIS
    when Prometheus is deployed and you want richer signals (per-container
    breakdown, network rx/tx, filesystem read/write). Equivalent to running
    PromQL via `kubectl exec` side-car pattern, automated.

    For cluster-wide CPU/memory ranking of many Pods (using metrics-server
    only, no Prometheus needed), use `top_pods` instead. For arbitrary PromQL,
    use `prometheus_query`. Requires a reachable Prometheus; use
    `find_prometheus_service` to locate one.

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


# RFC 1123 label regex + validator. Used by both `find_prometheus_service`
# upstream and `expose_prometheus_as_nodeport` here to refuse malformed
# inputs with a friendly message rather than a 422 from the apiserver.
_NAME_RE = re.compile(
    r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?(\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)*$"
)


def _validate_k8s_name(value: str, kind: str) -> None:
    """Validate a string is a legal RFC 1123 K8s resource name."""
    if not value or not _NAME_RE.match(value):
        raise ValueError(
            f"invalid {kind}={value!r}: must match RFC 1123 label "
            "(lowercase alphanum / '-', '.' allowed for some kinds)"
        )


# =============================================================================
# NodePort bridge — externally-reachable clone of a ClusterIP Service
# =============================================================================
#
# Why: ClusterIP Services (`10.96.x.x`) are unroutable from outside the
# cluster. A NodePort Service is a K8s primitive: kube-proxy binds the
# NodePort on every Node, traffic reaches the same backing Pods via the
# existing selector. If the cluster's Node IPs are network-reachable
# from the MCP client (typical for VPCs / private networks / corporate
# clusters), the Prometheus tools can hit `http://<node-ip>:<node_port>`
# directly — no extra processes, no extra binaries, and the URL works
# for every other tool the user has too.
#
# This tool creates a *parallel* Service of type=NodePort that points at
# the same Pods as the original ClusterIP Service. The original is left
# alone — in-cluster consumers keep using it, just like before.
# =============================================================================


# kube-prometheus-stack's Prometheus Service typically carries multiple
# ports on its spec (e.g. `http` + `reloader-web`, both forwarding to
# 9090 but with different names). Only the one named for the actual
# Prometheus HTTP API is useful to external callers — cloning every port
# wastes NodePort slots and gives the agent an ambiguous multi-port URL
# to deal with. So we pick exactly one ServicePort and let the
# apiserver allocate a single NodePort for it.
_PROMETHEUS_PORT_NAME_PREFERENCES: tuple[str, ...] = (
    "http", "web", "prometheus", "https",
)


def _pick_prometheus_target_port(
    ports: list[client.V1ServicePort],
) -> client.V1ServicePort | None:
    """Return the V1ServicePort that fronts the Prometheus HTTP API.

    Tries well-known names first (`http`, `web`, ...); falls back to the
    first port if no name matches (the user's own installs may use
    anything).
    """
    for pref in _PROMETHEUS_PORT_NAME_PREFERENCES:
        for p in ports or []:
            if (p.name or "").lower() == pref:
                return p
    return ports[0] if ports else None


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


def _build_prometheus_port_spec(p: client.V1ServicePort) -> client.V1ServicePort:
    """Build a V1ServicePort for the clone.

    `node_port` is *intentionally omitted*. The K8s apiserver is a single
    leader that serializes NodePort allocation against a global in-use
    set — passing an explicit value creates a TOCTOU race (we scan, we
    pick, we submit, *somebody else has just grabbed the same port*).
    Letting the apiserver pick guarantees uniqueness without any
    client-side coordination.
    """
    return client.V1ServicePort(
        name=p.name,
        port=p.port,
        target_port=p.target_port,
        protocol=p.protocol,
        # No node_port — apiserver allocates from 30000-32767.
    )


def expose_prometheus_as_nodeport(
    namespace: str,
    service_name: str,
    name_suffix: str = "-np",
) -> str:
    """**RECOMMENDED for ClusterIP Prometheus.** Create a parallel NodePort
    Service so the MCP client can reach it directly.

    Most Prometheus installs expose the API on a `ClusterIP` Service —
    a virtual IP (typically `10.96.x.x`) that is only routable from
    *inside* the cluster. The MCP server runs on the user's machine
    (Cherry Studio / Claude Desktop), where packets to `10.96.x.x` are
    dropped at the routing layer. This tool is the **preferred bridge**:
    it creates a parallel `NodePort` Service in the cluster with the same
    selector as the original, plus the single port that fronts the
    Prometheus HTTP API. The backing Pods stay accessible in two ways
    (in-cluster via the original ClusterIP; externally via the new
    NodePort). The original Service is never modified — in-cluster
    consumers (Prometheus itself, alertmanager, Grafana sidecars) keep
    working unchanged.

    Why this is the recommended cluster-IP → externally-reachable
    bridge:
      - **No external binary required.** Works on machines without
        `kubectl` on PATH (this MCP server has no other use for it).
      - **No TOCTOU race on the nodePort.** The K8s apiserver is a single
        leader that serializes port allocation against a global in-use
        set; passing `node_port=None` lets it auto-allocate atomically.
      - **No macOS / sandbox binding issues.** Subprocess-based bridges
        (kubectl port-forward) sometimes land on an IPv6-bound
        localhost and the agent gets `[Errno 61] Connection refused`
        even though the process reports success.

    If the original Service is already `NodePort` / `LoadBalancer` /
    `ExternalName`, this tool short-circuits and just returns the existing
    URL — no creation needed.

    **Why no explicit `node_port`**: passing a numeric value would create
    a scan-then-create race (TOCTOU) against other clients. The K8s
    apiserver is a single leader that serializes port allocation
    internally against a global in-use set; *not* setting `node_port`
    lets it allocate itself, which is guaranteed-unique.

    Args:
        namespace: where the Prometheus Service lives.
        service_name: the existing Service name (use
            `find_prometheus_service()` to find this).
        name_suffix: suffix for the new Service name. Default `-np`.
            Pass `""` to overwrite an in-place conversion (rare; you
            usually want the original left alone, so keep the suffix).

    Returns:
        A formatted summary table (NAMESPACE / SOURCE / TARGET / TYPE /
        NODE_PORT / URL), plus:

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
            + "\nUse this URL as `prometheus_url=`."
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

    # Pick the single port that fronts the Prometheus HTTP API; cloning
    # every port on the original Service wastes NodePort slots and gives
    # an ambiguous multi-URL answer.
    target_port = _pick_prometheus_target_port(orig_ports)
    if target_port is None:
        raise ValueError(
            f"Service {namespace}/{service_name} has no usable port. "
            "Refusing to create an empty NodePort Service."
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

    # 5. Create the new NodePort Service with NO explicit node_port. The
    #    apiserver will allocate one from the 30000-32767 range
    #    atomically against the global in-use set, so no client-side
    #    coordination is needed.
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
            ports=[_build_prometheus_port_spec(target_port)],
        ),
    )

    try:
        created = core.create_namespaced_service(namespace=namespace, body=body)
    except ApiException as e:
        # Surface a clean message; re-raise wrapped so upstream tracebacks
        # still have the original ApiException attached.
        logger.debug("create NodePort Service failed: %s", e)
        raise RuntimeError(
            f"Apiserver rejected NodePort Service {namespace}/{new_name} "
            f"(HTTP {e.status}): {(e.body or b'').decode('utf-8', errors='replace')}"
        ) from e

    # 6. Read back the apiserver-assigned nodePort from the returned
    #    object. We picked a single port so we expect a single number.
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
        f"Port: apiserver allocated nodePort={actual_port} "
        f"(internal port {target_port.port}, target {target_port.target_port}).\n"
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
    mcp.tool()(expose_prometheus_as_nodeport)
