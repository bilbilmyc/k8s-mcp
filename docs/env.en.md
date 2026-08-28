# Environment reference

[中文](./env.md) · [Documentation](./README.en.md)

Application variables use the `K8S_MCP_` prefix; standard `KUBECONFIG` is also supported. Pydantic settings are case-insensitive. Use an uncommitted `.env` file or a client `env` block.

## Runtime policy

| Variable | Default | Description |
| --- | --- | --- |
| `K8S_MCP_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |
| `K8S_MCP_DEFAULT_TAIL_LINES` | `100` | Default trailing lines for log tools, range 1–10000 |
| `K8S_MCP_READ_ONLY` | `false` | Read-only gate; set `true` to reject writes, patches, applies, and deletes |
| `K8S_MCP_NAMESPACE_ALLOWLIST` | unset | Comma-separated writable namespaces; required for production writes |
| `K8S_MCP_RATE_LIMIT_RPM` | `120` | Per-tool RPM; `0` disables it |
| `K8S_MCP_TOOL_TIMEOUT_S` | `60` | Seconds an MCP request waits; `0` disables it |
| `K8S_MCP_MAX_CONCURRENT_TOOLS` | `8` | Concurrent synchronous tools, range 1–64 |
| `K8S_MCP_ENABLED_GROUPS` | unset (all) | Comma-separated tool groups: `core` / `workload` / `observability` / `security` / `gpu` / `notify` (case-insensitive); unset registers all 91 tools, `ping` is always registered |

## Authentication

| Variable | Default | Description |
| --- | --- | --- |
| `K8S_MCP_API_SERVER` | unset | Direct Kubernetes API URL |
| `K8S_MCP_API_TOKEN` | unset | API bearer token; never commit it |
| `K8S_MCP_API_CA_CERT` | unset | Absolute CA certificate path |
| `K8S_MCP_API_INSECURE` | `false` | Skip TLS verification; controlled environments only |
| `K8S_MCP_KUBECONFIG` | unset | kubeconfig path |
| `K8S_MCP_KUBE_CONTEXT` | unset | kubeconfig context |
| `KUBECONFIG` | unset | Standard kubeconfig path or path list; lower priority than in-cluster auth and `K8S_MCP_KUBECONFIG` |

Outside a cluster, with none of these set, the default `~/.kube/config` is used. See [Quick start](./quickstart.en.md) for the complete precedence.

## Observability and bootstrap

| Variable | Default | Description |
| --- | --- | --- |
| `K8S_MCP_PROMETHEUS_URL` | unset | Explicit Prometheus URL; skips discovery |
| `K8S_MCP_PROMETHEUS_BEARER_TOKEN` | unset | Prometheus bearer token |
| `K8S_MCP_PROMETHEUS_NAMESPACE_ALLOWLIST` | unset | Sends Prometheus Service list requests only to these namespaces; unset performs one cluster-wide list |
| `K8S_MCP_LOCAL_PATH_PROVISIONER_URL` | Rancher `v0.0.32` manifest | Replace with a reviewed internal mirror for offline environments |
| `K8S_MCP_METRICS_SERVER_MANIFEST_URL` | metrics-server `v0.7.2` manifest | Replace with a reviewed internal mirror for offline environments |

`top_pods` and `top_nodes` never install components implicitly. When both read paths fail, they only recommend explicitly calling `bootstrap_metrics_server`. Its manifest contains cluster-scoped RBAC, so an instance with a namespace allowlist refuses the install.

## Webhooks

| Variable | Default | Description |
| --- | --- | --- |
| `K8S_MCP_NOTIFIERS` | unset | JSON array with `name`, `type`, `url`, optional `cluster_label` |
| `K8S_MCP_NOTIFIER_URL_ALLOW_HTTP` | `false` | Permit HTTP only for a deliberate local use case |
| `K8S_MCP_NOTIFIER_URL_ALLOWLIST` | unset | Comma-separated exact host allowlist; recommended in production |
| `K8S_MCP_NOTIFIER_ALLOW_PRIVATE_HOSTS` | `false` | Permit literal private IP hooks only for trusted internal endpoints |

## Minimal policy examples

The default kubeconfig and default read/write policy require no extra variables. A read-only session sets only:

```bash
export K8S_MCP_READ_ONLY=true
```

For production scoped writes, add only the target boundary:

```bash
export K8S_MCP_NAMESPACE_ALLOWLIST=staging,preview
```

Override performance values only after measurement shows the defaults are unsuitable:

```bash
export K8S_MCP_RATE_LIMIT_RPM=60
export K8S_MCP_TOOL_TIMEOUT_S=45
export K8S_MCP_MAX_CONCURRENT_TOOLS=4
```

Transport defaults require no configuration: Kubernetes HTTP connect/read timeouts are `5s/30s`; the connection pool is `max(8, MAX_CONCURRENT_TOOLS × 2)`; `ApiClient` and `DynamicClient` are reused in-process. `doctor` reports the effective values.
