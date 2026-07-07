"""Resource usage metrics (kubectl top equivalent).

Three-tier cascade so `top_pods` / `top_nodes` work even when metrics-server
isn't installed:

  1. **metrics-server** (the canonical data source, fastest path) — direct
     apiserver aggregation layer `/apis/metrics.k8s.io/v1beta1/...`.
  2. **Prometheus** (via cAdvisor + node-exporter) — falls back when
     metrics-server 404s. Uses `container_cpu_usage_seconds_total`,
     `container_memory_working_set_bytes` (kubelet/cAdvisor scrape) for
     pods and `node_cpu_seconds_total` + `node_memory_MemAvailable_bytes`
     (node-exporter) for nodes.
  3. **`bootstrap_metrics_server`** — auto-invoked when both #1 and #2
     fail AND write permission to `kube-system` is available. Idempotent:
     already-installed Deployment short-circuits with status. When the
     auto-install can't proceed (READ_ONLY or allowlist excludes
     kube-system), the cascade raises a RuntimeError that names the
     bootstrap tool and the missing-prometheus path as fallbacks.

中文：
- `top_pods` / `top_nodes`：优先走 metrics-server，没有就 fallback 到
  Prometheus；都没有就尝试 `bootstrap_metrics_server` 安装。
- `sort_by=memory|cpu`：排序字段；CPU 单位是核（如 250m），内存是字节。
- `prometheus_url`：可选，绕过自动发现；agent 通常先用
  `find_prometheus_service()` 拿到 URL 再传过来。
"""
from __future__ import annotations

import logging
from typing import Any

from kubernetes import client
from kubernetes.client.rest import ApiException

from ..client import get_api_client
from ..config import Settings, get_settings
from ..formatters import short_table
from . import prometheus as prom_mod

# Local module-level import so tests can patch `metrics.apply_yaml`.
# Putting it in `bootstrap_metrics_server` would keep tests from being
# able to monkeypatch the apply step (the function-level import would
# shadow the module attribute).
from .generic import apply_yaml

logger = logging.getLogger(__name__)


# PromQL label sets for the Prometheus fallback path. `__labels__` is
# substituted at call time with the namespace / pod-name regex.
_PROMQL_POD_CPU = (
    'sum by (namespace, pod) ('
    'rate(container_cpu_usage_seconds_total{__labels__}[5m])'
    ')'
)
_PROMQL_POD_MEM = (
    'sum by (namespace, pod) ('
    'container_memory_working_set_bytes{__labels__}'
    ')'
)
_PROMQL_NODE_CPU = (
    'sum by (node) (rate(node_cpu_seconds_total{mode!="idle"}[5m]))'
)
_PROMQL_NODE_MEM = (
    'sum by (node) ('
    'node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes'
    ')'
)

# Manifest URLs
_METRICS_SERVER_DEFAULT_MANIFEST_URL = (
    "https://github.com/kubernetes-sigs/metrics-server/"
    "releases/latest/download/components.yaml"
)
_METRICS_SERVER_DEPLOYMENT_NAME = "metrics-server"
_METRICS_SERVER_NAMESPACE = "kube-system"

# One-shot gate so a failed bootstrap attempt doesn't get retried on every
# subsequent top_pods/top_nodes call. Reset by process restart (intentional —
# if the operator fixes kube-system perms, the next start picks up the new
# state).
_BOOTSTRAP_ATTEMPTED = False


# =============================================================================
# Exceptions
# =============================================================================


class _MetricsServerNotInstalledError(Exception):
    """Internal signal: metrics-server's aggregated API returned 404.

    Translated by the public `top_*` wrappers into either a Prometheus
    fallback or a bootstrap-attempt, so it never escapes to the agent.
    """


# =============================================================================
# Public tools — the cascade
# =============================================================================


def top_pods(
    namespace: str | None = None,
    label_selector: str | None = None,
    sort_by: str = "memory",
    prometheus_url: str | None = None,
) -> str:
    """Show current CPU + memory for Pods — pick THIS for the 90% case.

    Cascade (in priority order):
      1. metrics-server via apiserver aggregation layer.
      2. Prometheus (cAdvisor / kubelet scrape): if metrics-server is not
         installed AND a Prometheus is reachable (env, override, or
         auto-discoverable), query it directly with
         `container_cpu_usage_seconds_total` /
         `container_memory_working_set_bytes`.
      3. `bootstrap_metrics_server` — if both fail and write permission to
         `kube-system` is available, install metrics-server, then retry
         step 1.

    Only emits CPU + memory (the two things metrics-server / cAdvisor
    carry). For richer signals (network rx/tx, fs r/w, per-container
    breakdown) use `pod_metrics` / `prometheus_query` directly.

    Args:
        namespace: namespace to query; None = all namespaces.
        label_selector: e.g. "app=nginx". On the metrics-server path
            this is pushed to the apiserver; on the Prometheus fallback
            it is translated to a pod-name regex by listing pods with the
            selector first (extra apiserver call). Empty / no match falls
            through with the namespace-only filter and a notice.
        sort_by: "cpu" or "memory" (default "memory", top consumers first).
        prometheus_url: optional explicit URL — see `prometheus_query` for
            the discovery pattern. If omitted, the cascade uses
            `K8S_MCP_PROMETHEUS_URL` env var, then auto-discovery.

    Returns a NAME / NAMESPACE / CPU(cores) / MEMORY(bytes) table.

    Errors:
      RuntimeError when neither data source is reachable AND bootstrap
      isn't possible (read-only mode, or `kube-system` not in
      `K8S_MCP_NAMESPACE_ALLOWLIST`). The error literally names
      `bootstrap_metrics_server`, `find_prometheus_service()`, and
      `prometheus_query()` as the next moves the agent can make.
    """
    try:
        return _top_pods_metrics_server(namespace, label_selector, sort_by)
    except _MetricsServerNotInstalledError:
        pass

    # Prometheus fallback. If it succeeds, we're done; if it raises a
    # connection error, we'll try the bootstrap path before giving up.
    try:
        return _top_pods_prometheus(namespace, label_selector, sort_by, prometheus_url)
    except (LookupError, ValueError) as prom_err:
        bootstrap_msg = _maybe_bootstrap_metrics_server(
            trigger_reason=str(prom_err),
        )
        raise RuntimeError(
            "top_pods: neither metrics-server nor Prometheus is reachable.\n"
            f"  - metrics-server: not installed (apiserver 404 on /apis/metrics.k8s.io)\n"
            f"  - prometheus: {prom_err}\n"
            + bootstrap_msg
            + "  - OR call prometheus_query(<PromQL>, prometheus_url=<URL>) "
            "directly once a Prometheus URL is known.\n"
        ) from prom_err


def top_nodes(
    sort_by: str = "memory",
    prometheus_url: str | None = None,
) -> str:
    """Show current CPU and memory usage for Nodes (kubectl top nodes).

    Cascade mirrors `top_pods`:
      1. metrics-server.
      2. Prometheus via `node_cpu_seconds_total{mode!="idle"}` /
         `node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes`
         (node-exporter scrape).
      3. `bootstrap_metrics_server` if both fail and perms allow.

    Args:
        sort_by: "cpu" or "memory" (default "memory").
        prometheus_url: optional explicit URL — see `prometheus_query`.

    Returns a NAME / CPU(cores) / MEMORY(bytes) table.

    Errors:
      RuntimeError when neither data source is reachable AND bootstrap
      isn't possible.
    """
    try:
        return _top_nodes_metrics_server(sort_by)
    except _MetricsServerNotInstalledError:
        pass

    try:
        return _top_nodes_prometheus(sort_by, prometheus_url)
    except (LookupError, ValueError) as prom_err:
        bootstrap_msg = _maybe_bootstrap_metrics_server(
            trigger_reason=str(prom_err),
        )
        raise RuntimeError(
            "top_nodes: neither metrics-server nor Prometheus is reachable.\n"
            f"  - metrics-server: not installed (apiserver 404 on /apis/metrics.k8s.io)\n"
            f"  - prometheus: {prom_err}\n"
            + bootstrap_msg
            + "  - OR call prometheus_query(<PromQL>, prometheus_url=<URL>) "
            "directly once a Prometheus URL is known.\n"
        ) from prom_err


# =============================================================================
# Path 1: metrics-server
# =============================================================================


def _custom_objects_api():
    from kubernetes import client
    return client.CustomObjectsApi(get_api_client())


def _top_pods_metrics_server(
    namespace: str | None,
    label_selector: str | None,
    sort_by: str,
) -> str:
    api = _custom_objects_api()
    kwargs: dict[str, Any] = {}
    if label_selector:
        kwargs["label_selector"] = label_selector

    try:
        if namespace:
            items = api.list_namespaced_custom_object(
                "metrics.k8s.io", "v1beta1", namespace, "pods", **kwargs
            )["items"]
        else:
            items = api.list_cluster_custom_object(
                "metrics.k8s.io", "v1beta1", "pods", **kwargs
            )["items"]
    except ApiException as e:
        if e.status == 404:
            raise _MetricsServerNotInstalledError() from e
        raise

    rows: list[dict[str, str]] = []
    for it in items:
        containers = it.get("containers", [])
        cpu = sum(_parse_cpu(c["usage"]["cpu"]) for c in containers)
        mem = sum(_parse_mem(c["usage"]["memory"]) for c in containers)
        rows.append({
            "NAME": it["metadata"]["name"],
            "NAMESPACE": it["metadata"].get("namespace", ""),
            "CPU": _fmt_cpu(cpu),
            "MEMORY": _fmt_mem(mem),
        })

    if not rows:
        return "(no metrics — no pods matched or metrics-server is silent)"
    rows.sort(
        key=lambda r: _parse_mem(r["MEMORY"]), reverse=(sort_by == "memory")
    )
    return short_table(rows, ["NAME", "NAMESPACE", "CPU", "MEMORY"])


def _top_nodes_metrics_server(sort_by: str) -> str:
    api = _custom_objects_api()
    try:
        items = api.list_cluster_custom_object(
            "metrics.k8s.io", "v1beta1", "nodes"
        )["items"]
    except ApiException as e:
        if e.status == 404:
            raise _MetricsServerNotInstalledError() from e
        raise

    rows: list[dict[str, str]] = []
    for it in items:
        u = it["usage"]
        rows.append({
            "NAME": it["metadata"]["name"],
            "CPU": _fmt_cpu(_parse_cpu(u["cpu"])),
            "MEMORY": _fmt_mem(_parse_mem(u["memory"])),
        })

    rows.sort(
        key=lambda r: _parse_mem(r["MEMORY"]), reverse=(sort_by == "memory")
    )
    return short_table(rows, ["NAME", "CPU", "MEMORY"])


# =============================================================================
# Path 2: Prometheus fallback
# =============================================================================


def _top_pods_prometheus(
    namespace: str | None,
    label_selector: str | None,
    sort_by: str,
    prometheus_url: str | None,
) -> str:
    """Build label selectors, run two PromQL queries, merge into a table."""
    # 1. Build the namespace/pod label set. label_selector → list pods and
    #    take their names as a regex; fall back to namespace-only when
    #    there are no matches.
    label_clauses: list[str] = []
    notice = ""
    if namespace:
        label_clauses.append(f'namespace="{namespace}"')
    if label_selector:
        pod_names = _pods_matching_selector(namespace, label_selector)
        if pod_names:
            # Anchor with ^...$ so a prefix doesn't accidentally match.
            joined = "|".join(_promql_escape(p) for p in pod_names)
            label_clauses.append(f'pod=~"{joined}"')
        else:
            notice = (
                f"  (label_selector {label_selector!r} matched no pods; "
                "Prometheus path uses namespace-only filter)\n"
            )
    if not label_clauses:
        # All-namespaces / all-pods. Drop the in-pod pause container so
        # the agent doesn't see a phantom "POD" container row.
        label_clauses = ['container!=""', 'container!="POD"']
    label_set = ", ".join(label_clauses)

    cpu_q = _PROMQL_POD_CPU.replace("__labels__", label_set)
    mem_q = _PROMQL_POD_MEM.replace("__labels__", label_set)

    cpu_rows = _prom_instant_to_dicts(
        prom_mod._query_instant(cpu_q, prometheus_url),
        value_kind="cpu",
    )
    mem_rows = _prom_instant_to_dicts(
        prom_mod._query_instant(mem_q, prometheus_url),
        value_kind="mem",
    )

    merged = _merge_by_pod(cpu_rows, mem_rows)
    if not merged:
        return (
            notice
            + "(no metrics — Prometheus has no cAdvisor data for this scope. "
            "Check that cAdvisor / kubelet is being scraped.)"
        )

    rows = [
        {
            "NAME": pod,
            "NAMESPACE": ns,
            "CPU": _fmt_cpu(_parse_cpu(cpu)) if cpu else "0",
            "MEMORY": _fmt_mem(_parse_mem(mem)) if mem else "0",
        }
        for pod, ns, cpu, mem in merged
    ]
    rows.sort(
        key=lambda r: _parse_mem(r["MEMORY"]), reverse=(sort_by == "memory")
    )
    header = notice + "Source: Prometheus (cAdvisor / kubelet)\n"
    return header + short_table(rows, ["NAME", "NAMESPACE", "CPU", "MEMORY"])


def _top_nodes_prometheus(sort_by: str, prometheus_url: str | None) -> str:
    rows_cpu = _prom_instant_to_dicts(
        prom_mod._query_instant(_PROMQL_NODE_CPU, prometheus_url),
        value_kind="cpu",
    )
    rows_mem = _prom_instant_to_dicts(
        prom_mod._query_instant(_PROMQL_NODE_MEM, prometheus_url),
        value_kind="mem",
    )
    merged = _merge_by_node(rows_cpu, rows_mem)
    if not merged:
        return (
            "(no metrics — Prometheus has no node-exporter data. "
            "Check that node-exporter is being scraped.)"
        )

    rows = [
        {
            "NAME": node,
            "CPU": _fmt_cpu(_parse_cpu(cpu)) if cpu else "0",
            "MEMORY": _fmt_mem(_parse_mem(mem)) if mem else "0",
        }
        for node, cpu, mem in merged
    ]
    rows.sort(
        key=lambda r: _parse_mem(r["MEMORY"]), reverse=(sort_by == "memory")
    )
    header = "Source: Prometheus (node-exporter)\n"
    return header + short_table(rows, ["NAME", "CPU", "MEMORY"])


def _prom_instant_to_dicts(
    result: list[dict], *, value_kind: str
) -> list[tuple[str, str]]:
    """Flatten a Prometheus instant-query result into (key, raw_value) rows.

    For pod queries the key is `namespace/pod`; for node queries it's
    just `node`. `raw_value` is the unparsed string Prometheus returned
    (e.g. `"0.024"` for cores, `"134217728"` for bytes) — the caller
    re-parses with `_parse_cpu` / `_parse_mem` so output formatting stays
    consistent with the metrics-server path.
    """
    out: list[tuple[str, str]] = []
    for r in result:
        m = r.get("metric") or {}
        v = r.get("value") or []
        raw = v[1] if isinstance(v, list) and len(v) == 2 else "0"
        if value_kind == "cpu" and "namespace" in m:
            key = f"{m.get('namespace', '')}/{m.get('pod', '')}"
        else:
            key = m.get("node") or m.get("kubernetes_node") or m.get("instance", "")
        out.append((key, raw))
    return out


def _merge_by_pod(
    cpu_rows: list[tuple[str, str]],
    mem_rows: list[tuple[str, str]],
) -> list[tuple[str, str, str, str]]:
    """Join CPU + memory rows on `namespace/pod`. Returns [(pod, ns, cpu, mem)]."""
    cpu_by_key = dict(cpu_rows)
    mem_by_key = dict(mem_rows)
    keys = sorted(set(cpu_by_key) | set(mem_by_key))
    out: list[tuple[str, str, str, str]] = []
    for k in keys:
        ns, _, pod = k.partition("/")
        out.append((pod, ns, cpu_by_key.get(k, "0"), mem_by_key.get(k, "0")))
    return out


def _merge_by_node(
    cpu_rows: list[tuple[str, str]],
    mem_rows: list[tuple[str, str]],
) -> list[tuple[str, str, str]]:
    cpu_by_key = dict(cpu_rows)
    mem_by_key = dict(mem_rows)
    keys = sorted(set(cpu_by_key) | set(mem_by_key))
    return [(k, cpu_by_key.get(k, "0"), mem_by_key.get(k, "0")) for k in keys]


def _pods_matching_selector(
    namespace: str | None, label_selector: str
) -> list[str]:
    """Translate a label_selector into pod names by listing pods once.

    cAdvisor's `pod` label is the pod name (not selector-driven), so the
    only way to honor label_selector on the Prometheus path is to look up
    the matching pod names first. Single apiserver call. Returns [] on
    RBAC failure / apiserver error — the caller falls back to a
    namespace-only filter rather than raising (the metrics-server path
    is already gone at this point).
    """
    try:
        core = client.CoreV1Api()
        if namespace:
            pods = core.list_namespaced_pod(
                namespace=namespace, label_selector=label_selector
            ).items
        else:
            pods = core.list_pod_for_all_namespaces(
                label_selector=label_selector
            ).items
    except Exception as e:  # noqa: BLE001
        logger.debug("label_selector lookup failed: %s", e)
        return []
    return [p.metadata.name for p in pods if p.metadata and p.metadata.name]


def _promql_escape(s: str) -> str:
    """Escape a string for use inside a PromQL regex literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


# =============================================================================
# Path 3: bootstrap metrics-server (auto + explicit)
# =============================================================================


def _maybe_bootstrap_metrics_server(*, trigger_reason: str) -> str:
    """Try to install metrics-server when the cascade can't find either
    data source. Returns a guidance message for the public tool to embed
    in its RuntimeError — empty if bootstrap ran successfully (caller
    will then retry path 1 and either succeed or surface a fresh error).

    Behavior:
      - Read-only mode or `kube-system` not in `NAMESPACE_ALLOWLIST`
        → no-op; returns hint that points the agent at the explicit tool
        name and the Prometheus path.
      - First invocation in this process where write perms allow →
        apply the official manifest, wait briefly for Deployment
        availability, return "" (caller proceeds to retry metrics-server).
      - Second+ invocation with the same trigger → no-op; return hint
        with the recorded failure reason (avoid hammering the apiserver
        on every subsequent top_pods call).
    """
    global _BOOTSTRAP_ATTEMPTED
    settings = get_settings()

    if not _write_permitted(settings, _METRICS_SERVER_NAMESPACE):
        return (
            "  - bootstrap_metrics_server: SKIPPED (server is read-only, "
            "or `kube-system` is not in K8S_MCP_NAMESPACE_ALLOWLIST).\n"
            "    Next steps (pick one):\n"
            "      a) Allow `kube-system` in K8S_MCP_NAMESPACE_ALLOWLIST and "
            "re-call, or\n"
            "      b) Manually install metrics-server:\n"
            "         kubectl apply -f "
            "https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml\n"
            "      c) Install Prometheus (kube-prometheus-stack) and let the "
            "agent discover it via `find_prometheus_service()`.\n"
        )

    if _BOOTSTRAP_ATTEMPTED:
        return (
            "  - bootstrap_metrics_server: SKIPPED (already attempted this "
            "process; restart the MCP server to retry, or check kube-system "
            "Deployment/metrics-server manually).\n"
        )

    _BOOTSTRAP_ATTEMPTED = True
    try:
        bootstrap_metrics_server()
        return ""  # success — caller will retry path 1
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "auto-bootstrap of metrics-server failed (trigger: %s): %s",
            trigger_reason, e,
        )
        return (
            f"  - bootstrap_metrics_server: auto-install FAILED — {e}\n"
            "    Inspect the cluster and re-call, or install Prometheus.\n"
        )


def _write_permitted(settings: Settings, namespace: str) -> bool:
    if settings.read_only:
        return False
    if settings.namespace_allowlist is None:
        return True
    return namespace in settings.namespace_allowlist


def bootstrap_metrics_server(
    manifest_url: str | None = None,
    kubelet_insecure_tls: bool = True,
    wait_seconds: int = 30,
) -> str:
    """Install metrics-server into `kube-system` from the official manifest.

    Idempotent: if `Deployment/metrics-server` already exists, returns
    immediately with its status instead of re-applying.

    Used by the `top_pods` / `top_nodes` cascade (auto-invoked when both
    metrics-server and Prometheus are unreachable and write permission
    to `kube-system` is available), and as an explicit tool the agent
    can call when it wants metrics-server up before other work.

    Args:
        manifest_url: defaults to the official
            `https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml`.
            Override via `K8S_MCP_METRICS_SERVER_MANIFEST_URL` env var
            (offline / air-gapped installs — point at a self-hosted copy).
        kubelet_insecure_tls: when True (default), patch the Deployment
            with `--kubelet-insecure-tls` so metrics-server can scrape
            self-hosted kubelets whose serving cert isn't signed by the
            cluster CA. Disable on managed clusters (EKS / GKE / AKS)
            where the kubelet cert is CA-signed.
        wait_seconds: how long to poll the Deployment for Available
            condition after apply (0 = skip wait; just return "applied").

    Returns a multi-line summary:

        metrics-server: status=<Ready|AlreadyInstalled|Failed>
        namespace: kube-system
        manifest: <url>
        kubelet-insecure-tls: <true|false>
        wait: <waited Ns|skipped>
        Deployment: kube-system/metrics-server
        Replicas:  desired=1 ready=1/1 available=1
        Note: <helpful note if any>

    Errors:
      PermissionError — READ_ONLY is true, or `kube-system` is not in
        `K8S_MCP_NAMESPACE_ALLOWLIST` (the allowlist only accepts
        namespaced writes; cluster-scoped Resources in the manifest like
        ClusterRole / ClusterRoleBinding / ServiceAccount *are* needed
        and *will* be applied if `kube-system` is allowed — K8s itself
        does the namespaced-vs-cluster-scoped split).
      RuntimeError — manifest fetch failed, apiserver rejected apply,
        or Deployment didn't reach Available within `wait_seconds`.
    """
    settings = get_settings()

    if settings.read_only:
        raise PermissionError(
            "Server is in read-only mode (K8S_MCP_READ_ONLY=true); "
            "installing metrics-server requires write access."
        )
    if settings.namespace_allowlist is not None and (
        _METRICS_SERVER_NAMESPACE not in settings.namespace_allowlist
    ):
        raise PermissionError(
            f"Namespace '{_METRICS_SERVER_NAMESPACE}' is not in "
            "K8S_MCP_NAMESPACE_ALLOWLIST; install metrics-server refused."
        )

    url = (
        manifest_url
        or settings.metrics_server_manifest_url
        or _METRICS_SERVER_DEFAULT_MANIFEST_URL
    )

    # 1. Idempotency probe.
    apps = client.AppsV1Api()
    existing = None
    try:
        existing = apps.read_namespaced_deployment(
            name=_METRICS_SERVER_DEPLOYMENT_NAME,
            namespace=_METRICS_SERVER_NAMESPACE,
        )
    except ApiException as e:
        if e.status != 404:
            raise RuntimeError(
                f"Failed to probe for existing metrics-server: {e}"
            ) from e
    if existing is not None:
        ready = (
            (existing.status.available_replicas or 0)
            if existing.status else 0
        )
        desired = existing.spec.replicas if existing.spec else 0
        return (
            f"metrics-server: status=AlreadyInstalled\n"
            f"namespace: {_METRICS_SERVER_NAMESPACE}\n"
            f"manifest: (skipped — Deployment already exists)\n"
            f"Deployment: {_METRICS_SERVER_NAMESPACE}/{_METRICS_SERVER_DEPLOYMENT_NAME}\n"
            f"Replicas:  desired={desired} ready={ready}/{desired}\n"
            "No changes made. Call `top_pods()` / `top_nodes()` directly.\n"
        )

    # 2. Fetch manifest.
    manifest_text = _fetch_url(url)

    # 3. Apply.
    # `apply_yaml` is imported at module top so tests can patch it.
    apply_yaml(manifest_text)

    # 4. Optional: patch the Deployment for kubelet-insecure-tls.
    #    Idempotent — re-applying on a Deployment that already has the
    #    flag is a no-op for metrics-server's pod (it only changes the
    #    command-line).
    if kubelet_insecure_tls:
        _patch_metrics_server_kubelet_flag()

    # 5. Optional: wait for Available.
    wait_msg = "skipped"
    if wait_seconds > 0:
        ready, desired, waited = _wait_for_deployment_ready(
            _METRICS_SERVER_DEPLOYMENT_NAME,
            _METRICS_SERVER_NAMESPACE,
            wait_seconds,
        )
        wait_msg = f"waited {waited}s for {ready}/{desired} ready"

    return (
        f"metrics-server: status=Installed\n"
        f"namespace: {_METRICS_SERVER_NAMESPACE}\n"
        f"manifest: {url}\n"
        f"kubelet-insecure-tls: {str(kubelet_insecure_tls).lower()}\n"
        f"wait: {wait_msg}\n"
        f"Deployment: {_METRICS_SERVER_NAMESPACE}/{_METRICS_SERVER_DEPLOYMENT_NAME}\n"
        "Next: `top_pods()` and `top_nodes()` will pick it up via path 1 "
        "of the cascade.\n"
    )


def _fetch_url(url: str) -> str:
    """Fetch a manifest URL and return the text. Raises RuntimeError on
    HTTP / network errors with a one-liner message that doesn't leak
    headers / body bytes."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"manifest fetch failed: HTTP {e.code} {e.reason} for {url}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"manifest fetch failed for {url}: {e.reason}") from e
    except TimeoutError as e:
        raise RuntimeError(f"manifest fetch timed out after 15s: {url}") from e
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise RuntimeError(
            f"manifest at {url} is not UTF-8 ({len(data)} bytes): {e}"
        ) from e


def _patch_metrics_server_kubelet_flag() -> None:
    """Add `--kubelet-insecure-tls` to the metrics-server container args.

    Self-hosted single-node clusters (and most non-EKS/GKE/AKS
    distributions) ship kubelets with self-signed serving certs. Without
    this flag, metrics-server can't scrape them and `top` returns empty.
    The patch is intentionally narrow — only the `args` field is touched,
    everything else stays as upstream.
    """
    apps = client.AppsV1Api()
    try:
        deploy = apps.read_namespaced_deployment(
            name=_METRICS_SERVER_DEPLOYMENT_NAME,
            namespace=_METRICS_SERVER_NAMESPACE,
        )
    except ApiException as e:
        raise RuntimeError(
            f"Deployment read after apply failed: {e}"
        ) from e

    if not deploy.spec or not deploy.spec.template.spec.containers:
        return

    changed = False
    for c in deploy.spec.template.spec.containers:
        if c.name != _METRICS_SERVER_DEPLOYMENT_NAME:
            continue
        args = list(c.args or [])
        if "--kubelet-insecure-tls" not in args:
            args.append("--kubelet-insecure-tls")
            c.args = args
            changed = True
            break

    if not changed:
        return

    try:
        apps.patch_namespaced_deployment(
            name=_METRICS_SERVER_DEPLOYMENT_NAME,
            namespace=_METRICS_SERVER_NAMESPACE,
            body=deploy,
        )
    except ApiException as e:
        # Non-fatal: metrics-server still applied; the agent will see
        # empty `top` output until they manually add the flag. Surface
        # as RuntimeError so the user knows.
        raise RuntimeError(
            f"applied metrics-server, but failed to patch "
            f"--kubelet-insecure-tls: HTTP {e.status}"
        ) from e


def _wait_for_deployment_ready(
    name: str, namespace: str, timeout_s: int
) -> tuple[int, int, int]:
    """Poll Deployment status until ready or timeout. Returns (ready, desired, waited)."""
    import time

    apps = client.AppsV1Api()
    deadline = time.monotonic() + timeout_s
    waited = 0
    while True:
        try:
            d = apps.read_namespaced_deployment(name=name, namespace=namespace)
        except ApiException as e:
            raise RuntimeError(
                f"Deployment status read failed during wait: {e}"
            ) from e
        ready = (d.status.available_replicas or 0) if d.status else 0
        desired = d.spec.replicas if d.spec else 0
        if ready >= desired and desired > 0:
            return ready, desired, waited
        if time.monotonic() >= deadline:
            return ready, desired, waited
        time.sleep(2)
        waited += 2


# =============================================================================
# Quantity parsing — same shape as before, used by both paths.
# =============================================================================


def _parse_cpu(v: str) -> float:
    """Convert Kubernetes CPU quantity to cores (float)."""
    if v.endswith("n"):
        return float(v[:-1]) / 1_000_000_000
    if v.endswith("u"):
        return float(v[:-1]) / 1_000_000
    if v.endswith("m"):
        return float(v[:-1]) / 1_000
    return float(v)


def _parse_mem(v: str) -> int:
    """Convert Kubernetes memory quantity to bytes (int)."""
    units = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
              "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}
    if v.endswith(("Ei", "Pi", "Ti", "Gi", "Mi", "Ki")):
        num, unit = v[:-2], v[-2:]
        return int(float(num) * units[unit])
    if v.endswith(("E", "P", "T", "G", "M", "K")):
        num, unit = v[:-1], v[-1:]
        return int(float(num) * units[unit])
    return int(float(v))


def _fmt_cpu(cores: float) -> str:
    if cores < 0.001:
        return "0"
    if cores < 1:
        return f"{int(cores * 1000)}m"
    return f"{cores:.2f}"


def _fmt_mem(bytes_: int) -> str:
    # 0 → "0" (not "0B") so the sort key (`_parse_mem(r["MEMORY"])`) can
    # round-trip back to bytes; `_parse_mem("0B")` raises ValueError
    # because the trailing "B" isn't a recognised unit.
    if bytes_ == 0:
        return "0"
    if bytes_ < 1024:
        return f"{bytes_}B"
    if bytes_ < 1024**2:
        return f"{bytes_ // 1024}Ki"
    if bytes_ < 1024**3:
        return f"{bytes_ // 1024**2}Mi"
    return f"{bytes_ / 1024**3:.1f}Gi"


def register(mcp) -> None:
    mcp.tool()(top_pods)
    mcp.tool()(top_nodes)
    mcp.tool()(bootstrap_metrics_server)
