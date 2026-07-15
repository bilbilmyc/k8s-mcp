# Environment reference

[中文](./env.md) · [Documentation](./README.en.md)

Every variable uses the `K8S_MCP_` prefix; Pydantic settings are case-insensitive. Use an uncommitted `.env` file or a client `env` block.

## Runtime policy

| Variable | Default | Description |
| --- | --- | --- |
| `K8S_MCP_LOG_LEVEL` | `INFO` | Logging level |
| `K8S_MCP_DEFAULT_TAIL_LINES` | `100` | Default trailing lines for log tools |
| `K8S_MCP_READ_ONLY` | `false` | Read-only gate; set `true` to reject writes, patches, applies, and deletes |
| `K8S_MCP_NAMESPACE_ALLOWLIST` | unset | Comma-separated writable namespaces; required for production writes |
| `K8S_MCP_RATE_LIMIT_RPM` | `120` | Per-tool RPM; `0` disables it |
| `K8S_MCP_TOOL_TIMEOUT_S` | `60` | Seconds an MCP request waits; `0` disables it |
| `K8S_MCP_MAX_CONCURRENT_TOOLS` | `8` | Concurrent synchronous tools, range 1–64 |

## Authentication

| Variable | Default | Description |
| --- | --- | --- |
| `K8S_MCP_API_SERVER` | unset | Direct Kubernetes API URL |
| `K8S_MCP_API_TOKEN` | unset | API bearer token; never commit it |
| `K8S_MCP_API_CA_CERT` | unset | Absolute CA certificate path |
| `K8S_MCP_API_INSECURE` | `false` | Skip TLS verification; controlled environments only |
| `K8S_MCP_KUBECONFIG` | unset | kubeconfig path |
| `K8S_MCP_KUBE_CONTEXT` | unset | kubeconfig context |

## Observability and bootstrap

| Variable | Default | Description |
| --- | --- | --- |
| `K8S_MCP_PROMETHEUS_URL` | unset | Explicit Prometheus URL; skips discovery |
| `K8S_MCP_PROMETHEUS_BEARER_TOKEN` | unset | Prometheus bearer token |
| `K8S_MCP_PROMETHEUS_NAMESPACE_ALLOWLIST` | unset | Limits namespaces scanned by Prometheus discovery |
| `K8S_MCP_LOCAL_PATH_PROVISIONER_URL` | Rancher `v0.0.32` manifest | Replace with a reviewed internal mirror for offline environments |
| `K8S_MCP_METRICS_SERVER_MANIFEST_URL` | metrics-server `v0.7.2` manifest | Replace with a reviewed internal mirror for offline environments |

## Webhooks

| Variable | Default | Description |
| --- | --- | --- |
| `K8S_MCP_NOTIFIERS` | unset | JSON array with `name`, `type`, `url`, optional `cluster_label` |
| `K8S_MCP_NOTIFIER_URL_ALLOW_HTTP` | `false` | Permit HTTP only for a deliberate local use case |
| `K8S_MCP_NOTIFIER_URL_ALLOWLIST` | unset | Comma-separated exact host allowlist; recommended in production |
| `K8S_MCP_NOTIFIER_ALLOW_PRIVATE_HOSTS` | `false` | Permit literal private IP hooks only for trusted internal endpoints |

## Scoped write example

```bash
export K8S_MCP_READ_ONLY=false
export K8S_MCP_NAMESPACE_ALLOWLIST=staging,preview
export K8S_MCP_RATE_LIMIT_RPM=60
export K8S_MCP_TOOL_TIMEOUT_S=45
export K8S_MCP_MAX_CONCURRENT_TOOLS=4
export K8S_MCP_NOTIFIER_URL_ALLOWLIST=hooks.slack.com,open.feishu.cn
```
