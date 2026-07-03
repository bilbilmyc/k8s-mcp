# k8s-mcp

Kubernetes MCP server for LLM agents. Exposes 30+ tools covering CRUD on
Pods, Deployments, StatefulSets, DaemonSets, Services, Ingresses, ConfigMaps
plus logs/events, node ops, top, rollout, wait, and bulk YAML apply.

The goal is to drive day-to-day K8s operations from natural language
(Claude Desktop, Cursor, Cline, …) with structured tool calls instead of
`kubectl` shell scraping.

## Quick start

```bash
# From source (dev mode)
uv sync
uv run k8s-mcp

# From a built wheel (production / agent config)
uv build
uv tool run --from ./dist/k8s_mcp-0.1.0-py3-none-any.whl k8s-mcp
```

That's it. By default the server reads `~/.kube/config`. Override via env
vars (see below).

## Authentication — three modes

Auto-detected, in this priority:

### Mode A — apiserver URL + token
For remote/CI/CD scenarios where you can't use a kubeconfig.

```bash
export K8S_MCP_API_SERVER=https://api.example.com:6443
export K8S_MCP_API_TOKEN=eyJhbGciOiJSUzI1NiIs...
export K8S_MCP_API_CA_CERT=/path/to/ca.crt   # optional
export K8S_MCP_API_INSECURE=false            # optional, skip TLS verify (testing only)
```

### Mode B — kubeconfig
Default. Reads `KUBECONFIG` env or `~/.kube/config`.

```bash
export KUBECONFIG=/path/to/kubeconfig         # optional
export K8S_MCP_KUBE_CONTEXT=my-cluster        # optional, override current-context
```

### Mode C — in-cluster
Auto-detected when `/var/run/secrets/kubernetes.io/serviceaccount/token`
exists. Useful when running the MCP server as a sidecar inside a pod.

## Claude Desktop / Cursor / Cline / Claude Code setup

`uv tool run --from <wheel>` is the recommended stable entrypoint because
every agent uses it the same way, regardless of how the source lives on
the box.

```json
{
  "mcpServers": {
    "k8s": {
      "command": "uv",
      "args": [
        "tool", "run", "--from",
        "/Users/mayc/codes/k8s-mcp/dist/k8s_mcp-0.1.0-py3-none-any.whl",
        "k8s-mcp"
      ],
      "env": {
        "K8S_MCP_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

For Claude Code, the equivalent registration is:

```bash
claude mcp add-json k8s "$(cat <<'EOF'
{
  "command": "uv",
  "args": ["tool", "run", "--from",
           "/Users/mayc/codes/k8s-mcp/dist/k8s_mcp-0.1.0-py3-none-any.whl",
           "k8s-mcp"],
  "env": { "K8S_MCP_LOG_LEVEL": "INFO" }
}
EOF
)"
```

To use Mode A instead, add `"K8S_MCP_API_SERVER"` and `"K8S_MCP_API_TOKEN"`
to the `env` block. Mode C (in-cluster) needs no env at all — it reads the
pod's SA token automatically.

Restart the agent. You should see ~46 tools listed under "k8s".

## Safety flags

```bash
# Read-only mode: all write tools refuse with PermissionError.
export K8S_MCP_READ_ONLY=true

# Namespace allowlist for writes. Reads are unrestricted.
# Cluster-scoped writes (no namespace) are rejected when this is set.
export K8S_MCP_NAMESPACE_ALLOWLIST=default,app,prod

# HMAC secret for delete confirmation tokens. CHANGE THIS in production.
export K8S_MCP_DELETE_TOKEN_SECRET=$(openssl rand -hex 32)

# Token TTL in seconds (default 300 = 5 min).
export K8S_MCP_DELETE_TOKEN_TTL_SECONDS=300
```

## Notifier webhooks

Push the output of read-only tools (typically `cluster_health_snapshot` /
`get_certificate_expiry`) to IM channels:

```bash
export K8S_MCP_NOTIFIERS='[
  {"name": "ops-feishu", "type": "feishu",
   "url": "https://open.feishu.cn/open-apis/bot/v2/hook/...",
   "cluster_label": "prod"},
  {"name": "oncall", "type": "slack",
   "url": "https://hooks.slack.com/services/...",
   "cluster_label": "prod"}
]'
```

Each entry: `{name, type, url, cluster_label?}`. `type` is `feishu` /
`slack` / `wecom` / `generic`; the `notify` tool assembles the per-type
JSON payload so the agent doesn't have to. `cluster_label` is prefixed
on the message so a single webhook can multiplex multiple clusters.

## Tool catalog (~50 tools)

### Read (always safe)
- `list_resources(kind, namespace?, label_selector?, api_version=None)` — list any Kind; **CRDs supported** (pass `api_version='cert-manager.io/v1'` etc.; required when the same Kind exists in multiple groups)
- `get_resource(kind, name, namespace?, api_version=None)` — full JSON object (CRD-aware)
- `get_resource_yaml(kind, name, namespace?, reveal_secrets=False, api_version=None)` — YAML manifest; Secrets are masked by default (CRD-aware)
- `describe_resource(kind, name, namespace?, api_version=None)` — kubectl-describe-style summary (CRD-aware)
- `get_resource_jsonpath(kind, path, name?, namespace?, label_selector?)` — extract one field
- `diff_resource(yaml_content)` — preview what apply_yaml would change (CREATE vs UPDATE, top-level field changes)
- `list_pods(namespace?, label_selector?, field_selector?, include_all=False)`
- `list_events(namespace?, field_selector?, warning_only=False, limit=50)`
- `get_pod_logs(pod_name|label_selector, namespace, container?, tail_lines?, since_seconds?, since_time=RFC3339?, until_time=RFC3339?, strict_time=False, previous=False, timestamps=False, pattern=regex?, context_lines=0, max_bytes=1MiB, output_format=text|json)` — empty result returns an informative notice, not blank. `since_time` is passed to the apiserver; `until_time` is enforced client-side (K8s has no `untilTime`); `strict_time=True` drops lines without parseable RFC3339 timestamps (useful for containers that don't emit them)
- `get_configmap(name, namespace)`
- `list_secrets(namespace?, label_selector?)` — metadata only, never returns values
- `get_secret_value(name, namespace, key, reveal=False)` — narrow blast-radius single-key fetch; reveal must be explicitly True
- `top_pods(namespace?, label_selector?, sort_by=memory|cpu)` — requires metrics-server
- `top_nodes(sort_by=memory|cpu)` — requires metrics-server
- `prometheus_query(promql, time?, prometheus_url?)` — Prometheus instant PromQL query (**not** metrics-server; queries any scraped series)
- `prometheus_query_range(promql, start, end, step="30s", prometheus_url?)` — Prometheus range query
- `pod_metrics(pod_name, namespace, metric="cpu|memory|network_rx|network_tx|fs_reads|fs_writes", range="5m", prometheus_url?)` — common cAdvisor-derived container metrics for a Pod (CPU / memory / network / fs IO)
- `find_prometheus_service(namespace=None)` — scans all (or one) namespace(s) for Services whose name looks like Prometheus; returns a **NAMESPACE / NAME / TYPE / RECOMMENDED / URL** table. For `TYPE=ClusterIP` rows the `RECOMMENDED` column literally contains `expose_prometheus_as_nodeport(namespace='<ns>', service_name='<name>')` — the agent copy-pastes that signature as the next call (no need to reason about which bridge to pick)
- `expose_prometheus_as_nodeport(namespace, service_name, name_suffix="-np")` — ⭐ **recommended for ClusterIP Prometheus**: **no `kubectl` needed**; creates a *parallel* `NodePort` Service (named `<original>-np`, with the same selector / labels as the original — only the `name=http|web|prometheus` port is cloned, to avoid wasting NodePort slots on neighboring reloader / grpc / health ports). The original ClusterIP Service is untouched. **The K8s apiserver allocates the nodePort itself** (atomic against a global in-use set; avoids the client-side scan-then-create race).
- `start_prometheus_port_forward(namespace, service_name, service_port=9090, local_port=None)` — **kubectl bridge fallback**: Prometheus Services are usually `ClusterIP` (`10.96.x.x`), only routable from inside the cluster. The MCP server runs *outside*, so hits get TCP RST. This tool launches a managed `kubectl port-forward` and returns a `127.0.0.1` URL the agent can use. **Requires `kubectl` on PATH**, and has known reliability issues on macOS sandboxes (IPv6 binding — `[Errno 61] Connection refused` even though `kubectl` reports success). **Use only when Node IPs are not reachable from the MCP client.**
- `list_port_forwards()` / `stop_port_forward(forward_id)` — list / terminate active forwards
- `rollout_status(kind, name, namespace, timeout_seconds=60, watch=False)` — polls until rollout completes
- `rollout_history(kind, name, namespace)` — list ControllerRevisions; pass revision to rollout_undo(to_revision=)
- `get_api_resources(prefix=None)` — list cluster kinds (CRDs included)
- `explain_resource(kind, field_path?, api_version?)` — `kubectl explain` over the OpenAPI schema
- `get_certificate_expiry()` — aggregate cluster-certificate expiry report. **The apiserver's own serving cert isn't queryable via the K8s API**, but the 4 sources the MCP server can see (`K8S_MCP_API_CA_CERT` / in-cluster SA bundle / kubeconfig CA / kubeconfig client cert — last one only when the kubeconfig uses cert auth) are read in one shot. Each row gives Subject / Issuer / NotBefore / NotAfter / days-left / status (✅ valid / ⚠️<30d / ❌<7d / ❌EXPIRED). Sorted ascending by days-left, with an "Action needed" block highlighting anything not yet expiring safely. **Local parse — no apiserver calls.**
- `cluster_health_snapshot(namespaces=None, events_minutes=60, restart_threshold=3)` — ⭐ **AI-ops entry point**: one call returns a 7-section cluster health report (Nodes / Pending Pods / Abnormal Restarts / HPA / Orphan PVs / Certificates / Recent Warning Events), with a `✅ HEALTHY` / `⚠️ ATTENTION` one-liner at the top. **Each section is independently error-bounded** — a single apiserver hiccup won't blank the whole report. Use this when asked "how's the cluster?"; drill into details with `describe_resource` / `get_pod_logs`.
- `notify(message, level="info", notifier_name=None, title=None)` — 📤 **proactive push**: POST any message (typically the output of `cluster_health_snapshot`) to one or more webhooks. **Webhook list is env-configured** (`K8S_MCP_NOTIFIERS='[{name, type, url, cluster_label?}, ...]'`); types `feishu` / `slack` / `wecom` / `generic` are supported, payload assembly is per-type. Returns a per-notifier `✅/❌` results table; HTTP errors don't raise (a dead webhook shouldn't take down the caller). `notifier_name` scopes to one; default broadcasts.

### Write (subject to read-only and namespace-allowlist)
- `apply_yaml(yaml_content)` — apply single or multi-doc manifest
- `replace_resource(yaml_content)` — PUT with ResourceVersion; refuses if cluster sees a newer revision
- `create_deployment(name, image, namespace?, replicas?, container_name?, ports?, env?, labels?, resources?, image_pull_policy?)`
- `create_statefulset(name, image, service_name, namespace?, replicas?, ...)`
- `create_service(...)`, `create_ingress(...)`, `expose_workload(...)`
- `create_hpa(name, target_kind, target_name, namespace, min_replicas, max_replicas, cpu_utilization?, memory_average_value?)`
- `create_pdb(name, target_kind, target_name, namespace, min_available=... | max_unavailable=...)`
- `create_role(name, namespace, rules)`, `create_rolebinding(name, namespace, role_kind, role_name, subjects)`
- `create_clusterrole(name, rules)`, `create_clusterrolebinding(name, role_name, subjects)`
- `create_serviceaccount(name, namespace, image_pull_secrets=[]?)`
- `create_networkpolicy(name, namespace, pod_selector, policy_types=[Ingress|Egress], ingress=[], egress=[])`
- `create_pvc(name, namespace, size, access_modes?, storage_class?, volume_name?, labels?)` — `volume_name` pins the PVC to a specific PV (hostPath / local volumes on dev/test clusters)
- `validate_pv_hostpath_paths()` — lists every hostPath PV with its target node + a one-line `ssh` check / create command (see "Troubleshooting & dev scenarios" below)
- `bootstrap_local_path_provisioner(set_as_default=True, apply_immediately=True)` — one-shot install of Rancher local-path provisioner for SC-less dev/test clusters (see "Troubleshooting & dev scenarios" below)
- `scale_workload(kind, name, namespace, replicas)`
- `restart_workload(kind, name, namespace)`
- `set_image(kind, name, namespace, container, image)`
- `set_resources(kind, name, namespace, container, requests={}, limits={})` — `kubectl set resources`
- `bulk_set_image(label_selector, container, image, kind=Deployment, namespace?, dry_run=True, confirm=False, confirmation_token?)` — ⚠️ bulk image update; dry-run → token → confirm flow
- `bulk_restart(label_selector, kind=Deployment, namespace?, dry_run=True, confirm=False, confirmation_token?)` — ⚠️ bulk rolling restart (stamps the `kubectl.kubernetes.io/restartedAt` annotation)
- `bulk_scale(label_selector, replicas, kind=Deployment, namespace?, dry_run=True, confirm=False, confirmation_token?)` — ⚠️ bulk `replicas` patch (Deployment / StatefulSet; DaemonSet rejected — no replicas concept)
- `rollout_undo(kind, name, namespace?, to_revision?)`
- `cordon_node(name)`, `uncordon_node(name)` — Node scheduling
- `drain_node(name, ignore_daemonsets=False, delete_emptydir_data=False, force=False, grace_period_seconds=-1, timeout_seconds=60)`
- `delete_pod(name, namespace, grace_period_seconds=30)` — recovery / restart primitive, bypasses 2-step confirm
- `wait_resource(kind, name, namespace?, for_condition=Ready|..., for_jsonpath=expr?, jsonpath_value?, timeout_seconds=60)`
- `update_configmap(name, namespace, data, merge=False)`

### Delete (two-step confirmation)
- `delete_resource(kind, name, namespace?, confirm=False, confirmation_token?, grace_period_seconds=30)`

### Notes on key tools

**`get_pod_logs`** is built for long-running pods (days/weeks of logs):

- Defaults: `tail_lines=100`, `max_bytes=1 MiB`.
- Use `pattern=<regex>` + `context_lines=N` to grep with N lines of context.
- Use `label_selector=...` to fetch logs for all matching pods at once
  (multi-pod mode prefixes each line with `[pod-name]`).
- Use `output_format=json` to get a list of `{pod, container, time, line}`
  records.
- Hard cap: 16 MiB; output is truncated from the head with a `[truncated]`
  footer if exceeded.
- When the container has no log output (writes to file / freshly started /
  too small tail), the tool returns an **explicit notice** rather than an
  empty string — prevents agents from missing the call entirely.

**`delete_resource`** uses a mandatory two-step flow:

1. Call `delete_resource(kind=..., name=..., namespace=..., confirm=False)`.
2. Tool returns `{preview_yaml, confirmation_token, expires_in_seconds}`.
3. Show the YAML to the user; ask for explicit confirmation.
4. Re-call with `confirm=True` and the `confirmation_token`. The token's
   payload (kind/name/namespace/grace_period) must match.

Tokens are HMAC-SHA256 signed (`K8S_MCP_DELETE_TOKEN_SECRET`), 5 min TTL.

**`bulk_set_image` / `bulk_restart` / `bulk_scale`** follow a **dry-run → token → confirm** three-step safety flow, because a single call can touch dozens of workloads:

1. `dry_run=True` (default) — list every resource matching `label_selector`; render a current→target diff table. **No write, no token issued.**
2. `dry_run=False, confirm=False` — same preview, plus a `confirmation_token` (HMAC-SHA256, 5 min TTL).
3. `dry_run=False, confirm=True, confirmation_token=...` — verify the token, then apply the change **only to the N resources that matched at preview time**. Resources that appeared with the same label_selector between preview and confirm are **NOT** touched — the token's `matched_names` list is the authoritative scope.

The token payload signs every "dangerous" parameter (image / container / replicas / label_selector / kind / namespace / op) — changing any one of them fails verification. A `bulk_set_image` token cannot unlock `bulk_scale`, and vice versa.

Workload type coverage:
- `bulk_set_image` / `bulk_restart`: Deployment / StatefulSet / DaemonSet
- `bulk_scale`: Deployment / StatefulSet only (DaemonSet has no `replicas` field — caller gets a clear ValueError pointing at `bulk_restart` instead)

**`drain_node`** mirrors `kubectl drain`:

- Cordon first; then evict pods via the Eviction API (respects PDBs).
- DaemonSet pods and emptyDir pods are skipped by default (matches kubectl);
  re-run with `ignore_daemonsets=True` / `delete_emptydir_data=True` to
  proceed.
- `force=True` bypasses PDBs (raw delete).

**Prometheus tools** (`prometheus_query` / `prometheus_query_range` /
`pod_metrics`) are a **separate system from `top_pods`**:

- `top_pods` calls Kubernetes's aggregation layer API at
  `/apis/metrics.k8s.io/...`, which **only sees metrics-server data**.
- The Prometheus tools hit Prometheus's HTTP API (default `:9090`), which
  sees every metric Prometheus has scraped (cAdvisor, node-exporter, app
  exporters / ServiceMonitors).
- Most Prometheus setups scrape cAdvisor by default, so
  `pod_metrics("nginx-7c5b", "default", "cpu")` works without
  metrics-server installed.

**Endpoint discovery** is a **collaboration** step — every cluster installs
Prometheus somewhere different (operator vs helm vs bare manifest, different
namespaces). k8s-mcp exposes a three-step protocol:

1. **Call `find_prometheus_service(namespace=None)` first** — scans every
   (or one) namespace for Services whose name looks like Prometheus
   (`prometheus` / `prometheus-operated` /
   `kube-prometheus-stack-prometheus` / `prometheus-server` / etc.) and
   returns a **NAMESPACE / NAME / TYPE / RECOMMENDED / URL** table. The
   `RECOMMENDED` column carries the exact call signature for the next
   step — copy it verbatim.
2. **Look at TYPE and follow the path:**

   - `TYPE=NodePort` / `LoadBalancer` → `RECOMMENDED` says `✅ direct`,
     the URL template is usable after substituting a Node / LB IP. Skip to step 3.
   - `TYPE=ClusterIP` (the default) → `RECOMMENDED` literally contains
     `expose_prometheus_as_nodeport(namespace='<ns>', service_name='<name>')`.
     Copy-paste that call: it creates a parallel NodePort Service
     (named `<svc>-np`) with the same selector — kube-proxy binds the
     NodePort on every Node automatically. The `nodePort` value is
     *not* set client-side; the K8s apiserver allocates it atomically
     against the global in-use set, so there's no scan-then-create
     TOCTOU race even under concurrent allocation pressure (which we
     hit earlier when scanning first then submitting collided with
     another client). **No `kubectl` required.** The agent fetches a
     Node IP via `list_resources(kind=Node)` and uses
     `http://<node-ip>:<node_port>`.
   - `TYPE=ClusterIP` AND Node IPs are not network-reachable (remote
     cluster, strict firewall, multi-hop NAT — common for managed K8s) →
     fall back to `start_prometheus_port_forward(namespace, service_name)`
     which starts a managed `kubectl port-forward` and returns a local
     `http://127.0.0.1:<port>` URL. **Requires `kubectl` on PATH**, and
     on macOS sandboxed clients can hit `[Errno 61] Connection refused`
     due to IPv6 binding — if so, restart the MCP server and retry.

3. **Pass that URL to the Prometheus tools** —
   `prometheus_query(promql, prometheus_url=<that URL>)` /
   `prometheus_query_range(..., prometheus_url=<URL>)` /
   `pod_metrics(..., prometheus_url=<URL>)`.

| Bridge | Recommended for | External deps | Long-lived process | Lifetime | Cleanup |
| --- | --- | --- | --- | --- | --- |
| `expose_prometheus_as_nodeport` | ⭐ ClusterIP (default) | none | no (K8s-native) | lives in the cluster until deleted | `delete_resource(kind="Service", name=<new>)` |
| `start_prometheus_port_forward` | Node IPs unreachable | `kubectl` binary | yes (subprocess) | dies with MCP server | `stop_port_forward(...)` |

If `K8S_MCP_PROMETHEUS_URL` is set, the tools use it directly and skip
discovery. There's also a small built-in fallback list of common
(namespace, Service) pairs; if even those fail, the tools return a
friendly "ask the user" message.

**Important constraints:**
  - `expose_prometheus_as_nodeport` is a write — it's refused in
    `K8S_MCP_READ_ONLY=true` mode and respects
    `K8S_MCP_NAMESPACE_ALLOWLIST`.
  - `start_prometheus_port_forward` only needs the apiserver, so it
    works even in read-only; it still honors the namespace allowlist
    (forwarding *into* a blocked namespace is rejected).

## Troubleshooting & dev scenarios

### Cluster has no StorageClass? Bootstrap a local one

kind / k3s default / minikube (without extras) ship **with no
StorageClass** — PVCs sit Pending forever. `bootstrap_local_path_provisioner`
solves it in one call:

```
bootstrap_local_path_provisioner()      # applies Rancher local-path-storage
```

After this, `storage_class_name="local-path"` works immediately —
PVCs auto-create hostPath PVs. **Don't use on production**
(hostPath is bound to the node; data is lost if the node dies).

Arguments:
- `set_as_default=True` (default) — mark the new SC as cluster-wide default
  so subsequent PVCs don't need `storage_class_name`.
- `apply_immediately=False` — return the manifest YAML without installing
  (good for auditing before applying).
- `K8S_MCP_LOCAL_PATH_PROVISIONER_URL` — air-gapped clusters, point at
  an internal mirror. Default: [Rancher official manifest](https://raw.githubusercontent.com/rancher/local-path-provisioner/master/deploy/local-path-storage.yaml).

Manifest is fetched and cached once per MCP session (re-fetched after
every client reconnect — see [[restart-clears-state]] memory note).

### Pod stuck on FailedMount? The hostPath directory may be missing

Dev/test clusters often use hand-rolled **hostPath PVs**
(`spec.hostPath.path=/data/xxx`). The kubelet does **not** create the
host directory — and a missing one looks like:

```
Warning  FailedMount  ... path "/data/k8s/pgsql-sts" does not exist
```

Fix flow:
1. `validate_pv_hostpath_paths()` — lists every hostPath PV, the node it's
   pinned to, and a one-line `ssh` command (checks `ls -ld`, then
   `sudo mkdir -p` if missing).
2. After fixing, the Pod auto-retries the mount.
3. `create_pvc(volume_name="...")` automatically appends a `mkdir -p`
   hint to its result when the bound PV is hostPath, so future calls
   don't repeat the gotcha.

When a PVC needs to bind to a specific hostPath PV, `volume_name` is
required — k8s does not match PVs to PVCs by hostPath path on its own
when no StorageClass is involved.

## End-to-end example (Claude session)

> You: "Deploy nginx 1.25 as a Deployment with 3 replicas, expose it via Service and Ingress."
>
> Claude → `create_deployment`, `expose_workload`, `create_ingress`.
>
> You: "Find any 5xx errors in the last hour."
>
> Claude → `get_pod_logs(label_selector=app=nginx, pattern=r"\b5\d\d\b",
> context_lines=2, since_seconds=3600)`.
>
> You: "Show me the request count from the HPA."
>
> Claude → `get_resource_jsonpath("HorizontalPodAutoscaler",
> "status.currentMetrics", name="web", namespace="default")`.
>
> You: "Wait until the deployment rolls out, then bump to 1.27."
>
> Claude → `wait_resource("Deployment", "nginx", namespace="default",
> for_condition="Available")` → `set_image(...)`.
>
> You: "Drain node-3 so I can reboot it."
>
> Claude → `cordon_node("node-3")` → lists pods → `drain_node("node-3")`.
>
> You: "Show me api-1's CPU and memory right now."
>
> Claude → `find_prometheus_service()` → reads the `RECOMMENDED` column
> (`expose_prometheus_as_nodeport(namespace='default',
> service_name='monitor-kube-prometheus-st-prometheus')`) and copy-pastes
> it → gets `node_port=31245` → `list_resources(kind='Node')` →
> Node IP `10.20.30.40` →
> `pod_metrics("api-1", "default", "cpu",
> prometheus_url="http://10.20.30.40:31245")` →
> `pod_metrics("api-1", "default", "memory",
> prometheus_url="http://10.20.30.40:31245")`.
>
> You: "Delete it."
>
> Claude → `delete_resource(confirm=False)` → shows you the YAML preview.
>
> You: "Yes, go ahead."
>
> Claude → `delete_resource(confirm=True, confirmation_token=...)`.

## Development

```bash
uv sync
uv run pytest              # 154 tests
uv run ruff check .        # lint
uv run k8s-mcp             # run over stdio
uv build                   # produce dist/*.whl + .tar.gz
```

## Architecture

```
src/k8s_mcp/
├── server.py         # FastMCP entry, registers all tools
├── config.py         # Settings (pydantic-settings, K8S_MCP_* env vars)
├── auth.py           # 3-mode auth (apiserver+token / kubeconfig / in-cluster)
├── client.py         # Cached ApiClient factory
├── formatters.py     # YAML / Table / Describe + Secret masking
├── safety.py         # HMAC confirmation tokens
└── tools/
    ├── generic.py    # list/get/get_yaml/describe/apply_yaml
    ├── workload.py   # create_deployment/statefulset, scale/restart/set_image
    ├── service.py    # create_service/ingress, expose_workload
    ├── logs.py       # get_pod_logs (long-log optimized)
    ├── pods.py       # list_pods
    ├── events.py     # list_events
    ├── configmap.py  # get/update_configmap
    ├── delete_tool.py# delete_resource (two-step)
    ├── metrics.py    # top_pods / top_nodes
    ├── rollout.py    # rollout_status / rollout_undo / rollout_history
    ├── node_ops.py   # cordon / uncordon / drain
    ├── wait_tool.py  # wait_resource (condition or JSONPath)
    ├── jsonpath.py   # get_resource_jsonpath
    ├── secret.py     # list_secrets + get_secret_value (single-key)
    ├── discovery.py  # get_api_resources + explain_resource
    ├── autoscale.py  # create_hpa + create_pdb
    ├── rbac.py       # Role / RoleBinding / ClusterRole / ClusterRoleBinding
    ├── serviceaccount.py # create_serviceaccount
    ├── networkpolicy.py # create_networkpolicy
    ├── storage.py    # create_pvc
    ├── prometheus.py # prometheus_query / prometheus_query_range / pod_metrics
    ├── health.py     # cluster_health_snapshot (7-section cluster health)
    ├── bulk.py       # bulk_set_image / bulk_restart / bulk_scale
    └── notifier.py   # notify (webhook push to feishu/slack/wecom/generic)
```

`generic.py` additionally exposes `replace_resource` (PUT with ResourceVersion)
and `diff_resource` (preview what apply would change).

See `PLAN.md` for the full design doc and `tests/` for examples.

## Programmatic usage (without MCP)

Every tool registered with FastMCP is also a plain Python function in
`k8s_mcp.tools.*`, so you can use the same building blocks from a script,
notebook, or CLI without spinning up an MCP server. Authentication,
safety, and namespace allowlist all still apply — they live in `config`,
`safety`, and per-tool checks, not in the MCP layer.

```python
# 程序化调用示例 —— 直接 import 函数，无需 MCP server
# 1) 加载配置（读取 K8S_MCP_* 环境变量）
from k8s_mcp.config import get_settings, reset_settings_cache
reset_settings_cache()  # 清掉可能的缓存
settings = get_settings()
print(settings.read_only, settings.namespace_allowlist)

# 2) 直接调一个 tool 函数 —— 与 MCP 工具签名完全一致
from k8s_mcp.tools import logs
result = logs.get_pod_logs(
    pod_name="nginx-7c5b-abc",
    namespace="default",
    tail_lines=50,
    pattern=r"\b5\d\d\b",      # 正则：抓 5xx 错误
    context_lines=2,           # 匹配前后各 2 行
    since_seconds=3600,        # 最近一小时
)
print(result)  # 纯文本，可直接进日志/告警

# 3) 时间窗口（绝对时间）—— "两点到四点之间"
from k8s_mcp.tools import logs
out = logs.get_pod_logs(
    pod_name="api-1",
    namespace="prod",
    since_time="2026-07-02T14:00:00Z",   # RFC3339，下界
    until_time="2026-07-02T16:00:00Z",   # RFC3339，上界（客户端过滤）
    pattern="aabbcc",
)

# 4) 创建资源 —— 走和 MCP 一样的守门（read-only / namespace allowlist）
from k8s_mcp.tools import workload
out = workload.create_deployment(
    name="web",
    image="nginx:1.25",
    namespace="default",
    replicas=3,
)
print(out)

# 5) 删除二次确认 —— 与 MCP 流程一致
from k8s_mcp.tools import generic as gen
# 第一步：不带 confirm，先拿到预览 + token
preview = gen.delete_resource(kind="Deployment", name="web", namespace="default")
print(preview)  # 含 confirmation_token
# 第二步：人工确认后，带 confirm=True + token 真正执行
# gen.delete_resource(kind="Deployment", name="web", namespace="default",
#                    confirm=True, confirmation_token="<token-from-preview>")
```

`k8s_mcp.client.get_api_client()` returns a cached
`kubernetes.client.api_client.ApiClient` honoring the same three auth
modes, so any code that wants to drop down to the raw kubernetes-python
client can do so and still get kubeconfig / apiserver-token / in-cluster
auto-detection.

## Out of scope (v2+)

- `exec_pod`, `port_forward` (stateful, doesn't fit MCP stdio)
- log streaming (same)
- Helm / Kustomize integration
- Multi-cluster routing
- MCP HTTP/SSE transport (v1 is stdio-only)
- Docker image / Helm chart publishing (we ship wheel only)
