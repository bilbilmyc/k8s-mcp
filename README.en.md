# k8s-mcp

Kubernetes MCP server for LLM agents. Exposes **74 tools** covering CRUD on
Pods, Deployments, StatefulSets, DaemonSets, Jobs, CronJobs, Services,
Ingresses, ConfigMaps, PVCs, RBAC, NetworkPolicies, plus logs/events, node
ops, top, rollout, wait, bulk YAML apply, Prometheus queries, health
snapshots, and proactive webhooks.

The goal is to drive day-to-day K8s operations from natural language
(Claude Desktop, Cursor, Cline, Cherry Studio, …) with structured tool
calls instead of `kubectl` shell scraping.

> **Package name note**: the PyPI name is `k8s-mcp-bilbilmyc` (a different
> 27-tool MCP already owns [`k8s-mcp`](https://pypi.org/project/k8s-mcp/)).
> The `import` name is still `k8s_mcp` and the CLI is still `k8s-mcp`.
> See [docs/publishing.md](./docs/publishing.md).

## Table of contents

- [Install](#install)
- [Authentication — three modes](#authentication--three-modes)
- [MCP client setup](#mcp-client-setup)
- [Safety flags](#safety-flags)
- [Notifier webhooks](#notifier-webhooks)
- [Tool catalog (74)](#tool-catalog-74)
- [Documentation index](#documentation-index)
- [Development](#development)

## Install

```bash
# 1) Install the CLI (once)
uv tool install k8s-mcp-bilbilmyc

# 2) Verify
k8s-mcp --help
```

**Or run it ephemerally (no install)**:

```bash
uvx --from k8s-mcp-bilbilmyc k8s-mcp
```

**From source (dev mode)**:

```bash
git clone https://github.com/bilbilmyc/k8s-mcp
cd k8s-mcp
uv sync
uv run k8s-mcp
```

By default the server reads `~/.kube/config`. Override via env vars (see
[docs/env.md](./docs/env.md)).

## Authentication — three modes

Auto-detected, in this priority:

### Mode A — apiserver URL + token
For remote / CI / CD scenarios where you can't use a kubeconfig.

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

## MCP client setup

> Recommended: install once via `uv tool install`, then every agent uses
> the same `command: k8s-mcp` entry. Upgrades don't touch agent config.

```json
{
  "mcpServers": {
    "k8s": {
      "command": "k8s-mcp",
      "env": {
        "K8S_MCP_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

**For Claude Code**:

```bash
claude mcp add-json k8s '{"command": "k8s-mcp", "env": {"K8S_MCP_LOG_LEVEL": "INFO"}}'
```

**To use Mode A instead**, add `"K8S_MCP_API_SERVER"` and `"K8S_MCP_API_TOKEN"`
to the `env` block. **Mode C** (in-cluster) needs no env at all — it reads
the pod's SA token automatically.

**Not installed yet?** Use `uvx` to run it without an install step:

```json
{
  "mcpServers": {
    "k8s": {
      "command": "uvx",
      "args": ["--from", "k8s-mcp-bilbilmyc", "k8s-mcp"],
      "env": { "K8S_MCP_LOG_LEVEL": "INFO" }
    }
  }
}
```

Restart the agent. You should see **74 tools** listed under "k8s".

Full environment variable reference: [docs/env.md](./docs/env.md).

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
JSON payload so the agent doesn't have to. `cluster_label` is prefixed on
the message so a single webhook can multiplex multiple clusters.

## Tool catalog (74)

> **New-session opening protocol**: the first two calls should always be
> `cluster_info()` → `whoami(namespace="<target ns>")`. The first tells
> you which apiserver, which K8s version, and the live counts; the second
> tells you which user/SA you are and exactly which resources/verbs you
> can touch in the target namespace. See [docs/tools.md](./docs/tools.md).

### Read (always safe)

**Generic queries**:

- `list_resources(kind, namespace?, label_selector?, api_version=None)` — list any Kind; CRD-aware
- `get_resource(kind, name, namespace?, api_version=None)` — full JSON object (CRD-aware)
- `get_resource_yaml(kind, name, namespace?, reveal_secrets=False, api_version=None)` — YAML manifest; Secrets masked by default
- `describe_resource(kind, name, namespace?, api_version=None)` — kubectl-describe-style summary
- `get_resource_jsonpath(kind, path, name?, namespace?, label_selector?)` — extract one field
- `diff_resource(yaml_content)` — preview what apply_yaml would change
- `get_api_resources(prefix=None)` — list cluster kinds (CRDs included)
- `explain_resource(kind, field_path?, api_version?)` — `kubectl explain` over the OpenAPI schema

**Identity / version / counts**:

- `cluster_info()` — apiserver URL / GitVersion / node & pod counts
- `whoami(namespace="default")` — current identity + effective namespace-scoped permissions

**Reverse lookup / triage**:

- `find_images(image_substring, namespace?, kinds?)` — substring search across workload images
- `get_events_for_object(kind, name, namespace?, limit=50)` — events for one object
- `list_pods(namespace?, label_selector?, field_selector?, include_all=False)`
- `list_events(namespace?, field_selector?, warning_only=False, limit=50)`
- `get_pod_logs(pod_name|label_selector, namespace, container?, tail_lines?, since_seconds?, since_time=RFC3339?, until_time=RFC3339?, strict_time=False, previous=False, timestamps=False, pattern=regex?, context_lines=0, max_bytes=1MiB, output_format=text|json)` — see [tools.md → get_pod_logs](./docs/tools.md#get_pod_logs)
- `get_configmap(name, namespace)`
- `list_secrets(namespace?, label_selector?)` — metadata only
- `get_secret_value(name, namespace, key, reveal=False)` — narrow single-key fetch

**Metrics / monitoring**:

- `top_pods(namespace?, label_selector?, sort_by=memory|cpu)` — metrics-server
- `top_nodes(sort_by=memory|cpu)` — metrics-server
- `prometheus_query(promql, time?, prometheus_url?)` — Prometheus instant query
- `prometheus_query_range(promql, start, end, step="30s", prometheus_url?)` — range query
- `pod_metrics(pod_name, namespace, metric="cpu|memory|network_rx|network_tx|fs_reads|fs_writes", range="5m", prometheus_url?)` — cAdvisor-derived
- `find_prometheus_service(namespace=None)` — scan Prometheus services + literal next-call signature
- `expose_prometheus_as_nodeport(namespace, service_name, name_suffix="-np")` — ⭐ recommended for ClusterIP
- `start_prometheus_port_forward(namespace, service_name, service_port=9090, local_port=None)` — fallback when Node IPs unreachable
- `list_port_forwards()` / `stop_port_forward(forward_id)` — manage forwards

**Ops**:

- `rollout_status(kind, name, namespace, timeout_seconds=60, watch=False)`
- `rollout_history(kind, name, namespace)` — pass to `rollout_undo(to_revision=)`
- `get_certificate_expiry()` — aggregate kubeconfig / SA bundle / apiserver CA certs
- `cluster_health_snapshot(namespaces=None, events_minutes=60, restart_threshold=3)` — ⭐ 7-section cluster health
- `notify(message, level="info", notifier_name=None, title=None)` — proactive webhook push

### Write (subject to read-only and namespace-allowlist)

**Apply**:

- `apply_yaml(yaml_content)` — single or multi-doc manifest
- `replace_resource(yaml_content)` — PUT with ResourceVersion

**Workloads**:

- `create_deployment(name, image, namespace?, replicas?, container_name?, ports?, env?, labels?, resources?, image_pull_policy?)`
- `create_statefulset(name, image, service_name, namespace?, replicas?, ...)`
- `create_job(name, image, namespace?, command?, args?, env?, resources?, restart_policy="Never", backoff_limit?)` — one-off task
- `create_cronjob(name, image, schedule, namespace?, command?, args?, env?, resources?, restart_policy="OnFailure")` — scheduled task

**Service / Ingress**:

- `create_service(...)`, `create_ingress(...)`, `expose_workload(...)`

**Storage**:

- `create_pvc(name, namespace, size, access_modes?, storage_class?, volume_name?, labels?)` — `volume_name` pins to a specific PV
- `validate_pv_hostpath_paths()` — list hostPath PVs + one-line ssh commands
- `bootstrap_local_path_provisioner(set_as_default=True, apply_immediately=True)` — install local-path for SC-less dev/test clusters

**Ops**:

- `scale_workload(kind, name, namespace, replicas)`
- `restart_workload(kind, name, namespace)`
- `set_image(kind, name, namespace, container, image)`
- `set_resources(kind, name, namespace, container, requests={}, limits={})`
- `bulk_set_image / bulk_restart / bulk_scale(label_selector, ..., dry_run=True, confirm=False, confirmation_token?)` — 3-step safe flow
- `rollout_undo(kind, name, namespace?, to_revision?)`
- `wait_resource(kind, name, namespace?, for_condition=..., for_jsonpath=expr?, jsonpath_value?, timeout_seconds=60)`
- `update_configmap(name, namespace, data, merge=False)`
- `cordon_node(name)`, `uncordon_node(name)` — node scheduling
- `drain_node(name, ignore_daemonsets=False, delete_emptydir_data=False, force=False, grace_period_seconds=-1, timeout_seconds=60)`

**RBAC / NetworkPolicy / ServiceAccount**:

- `create_role / create_rolebinding / create_clusterrole / create_clusterrolebinding`
- `create_serviceaccount(name, namespace, image_pull_secrets=[]?)`
- `create_networkpolicy(name, namespace, pod_selector, policy_types=[Ingress|Egress], ingress=[], egress=[])`
- `create_hpa / create_pdb`

### Delete

Three groups, by risk / confirmation level (see
[docs/tools.md → delete confirmation](./docs/tools.md#删除二次确认)).

**Generic (two-step) — Secrets and anything that cascades**:

- `delete_resource(kind, name, namespace?, confirm=False, confirmation_token?, grace_period_seconds=30)`

**One-step (recoverable, no cascade)**:

- `delete_pod(name, namespace, grace_period_seconds=30)` — recovery / restart primitive
- `delete_pvc(name, namespace)` — workload goes Pending until rebound
- `delete_configmap(name, namespace="default")` — losing a CM makes mounting Pods fail to start; CM is re-creatable
- `delete_service(name, namespace="default")` — a Service is a routing rule, not a workload
- `delete_ingress(name, namespace="default")` — external HTTP(S) drops, Pods / Services untouched

**Bulk (dry-run → token → confirm)**:

- `bulk_delete_pvc(label_selector, namespace=None, dry_run=True, confirm=False, confirmation_token?)` — clean up orphan PVCs

## Documentation index

| Doc | Content |
| --- | --- |
| [docs/env.md](./docs/env.md) | All `K8S_MCP_*` environment variables |
| [docs/tools.md](./docs/tools.md) | Detailed tool usage notes (new-session protocol, bulk 3-step, Prometheus bridge, …) |
| [docs/troubleshooting.md](./docs/troubleshooting.md) | Dev-cluster gotchas (no SC, hostPath, Forbidden, missing Prometheus) |
| [docs/examples.md](./docs/examples.md) | 13 end-to-end Claude / Cherry Studio dialogs |
| [docs/architecture.md](./docs/architecture.md) | Source tree + design notes |
| [docs/usage.md](./docs/usage.md) | Programmatic usage (no MCP server) |
| [docs/publishing.md](./docs/publishing.md) | PyPI / TestPyPI release workflow (token, uv publish, verify) |

## Development

```bash
uv sync
uv run pytest              # 419 tests
uv run ruff check .        # lint
uv run k8s-mcp             # run over stdio
uv build                   # produce dist/*.whl + .tar.gz
```

Release workflow: [docs/publishing.md](./docs/publishing.md). Full design
doc: [PLAN.md](./PLAN.md).

## Out of scope (v2+)

- `exec_pod`, `port_forward` (stateful, doesn't fit MCP stdio)
- log streaming (same)
- Helm / Kustomize integration
- Multi-cluster routing
- MCP HTTP / SSE transport (v1 is stdio-only)
- Docker image / Helm chart publishing
- CI + PyPI Trusted Publishing (v1 ships by hand)
