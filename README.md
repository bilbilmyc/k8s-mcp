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

## Tool catalog (~50 tools)

### Read (always safe)
- `list_resources(kind, namespace?, label_selector?)` — list any built-in Kind
- `get_resource(kind, name, namespace?)` — full JSON object
- `get_resource_yaml(kind, name, namespace?, reveal_secrets=False)` — YAML manifest; Secrets are masked by default
- `describe_resource(kind, name, namespace?)` — kubectl-describe-style summary
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
- `rollout_status(kind, name, namespace, timeout_seconds=60, watch=False)` — polls until rollout completes
- `rollout_history(kind, name, namespace)` — list ControllerRevisions; pass revision to rollout_undo(to_revision=)
- `get_api_resources(prefix=None)` — list cluster kinds (CRDs included)
- `explain_resource(kind, field_path?, api_version?)` — `kubectl explain` over the OpenAPI schema

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
- `create_pvc(name, namespace, size, access_modes?, storage_class?, labels?)`
- `scale_workload(kind, name, namespace, replicas)`
- `restart_workload(kind, name, namespace)`
- `set_image(kind, name, namespace, container, image)`
- `set_resources(kind, name, namespace, container, requests={}, limits={})` — `kubectl set resources`
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

**`drain_node`** mirrors `kubectl drain`:

- Cordon first; then evict pods via the Eviction API (respects PDBs).
- DaemonSet pods and emptyDir pods are skipped by default (matches kubectl);
  re-run with `ignore_daemonsets=True` / `delete_emptydir_data=True` to
  proceed.
- `force=True` bypasses PDBs (raw delete).

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
    └── storage.py    # create_pvc
```

`generic.py` additionally exposes `replace_resource` (PUT with ResourceVersion)
and `diff_resource` (preview what apply would change).

See `PLAN.md` for the full design doc and `tests/` for examples.

## Out of scope (v2+)

- `exec_pod`, `port_forward` (stateful, doesn't fit MCP stdio)
- log streaming (same)
- Helm / Kustomize integration
- Multi-cluster routing
- MCP HTTP/SSE transport (v1 is stdio-only)
- Docker image / Helm chart publishing (we ship wheel only)
