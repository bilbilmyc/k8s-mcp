# Security model

[中文](./security.md) · [Documentation](./README.en.md)

## Defaults

| Control | Default | Purpose |
| --- | --- | --- |
| `K8S_MCP_READ_ONLY` | `false` | Set to `true` to reject writes, patches, applies, and deletes |
| `K8S_MCP_NAMESPACE_ALLOWLIST` | unset | Scope writes to named namespaces once writes are enabled |
| `K8S_MCP_RATE_LIMIT_RPM` | `120` | Prevent a runaway loop from hammering one tool |
| `K8S_MCP_TOOL_TIMEOUT_S` | `60` | Bound how long an MCP request waits |
| `K8S_MCP_MAX_CONCURRENT_TOOLS` | `8` | Bound workers consumed by sync calls, including timed-out background work |
| Webhook HTTPS | required | Avoid cleartext message and credential exposure |

## Write authorization

1. Normal mode permits reads, writes, and deletes.
2. Reject every write immediately when `read_only=true`.
3. When an allowlist is configured, permit only listed namespaces and reject cluster-scoped writes.
4. Kubernetes RBAC remains the final authorization boundary; these guards do not replace RBAC.

Use separate read-only and write kubeconfigs. Bind the write kubeconfig only to namespace Roles it needs; see [Deployment](./deployment.en.md).

## Timeouts and concurrency

The Kubernetes Python client uses synchronous calls that Python cannot safely kill. When `tool_timeout_s` expires, MCP returns a recoverable error, but the underlying thread can still be waiting on the API server. It keeps its worker slot until it exits; when every slot is busy, new calls return a fast “server is busy” error instead of entering an unbounded queue.

For legitimate long operations, tune API-server/Prometheus timeouts and carefully raise the timeout and concurrency values rather than disabling safeguards.

## Webhook notifications

- Only `https://` is allowed by default. Set `K8S_MCP_NOTIFIER_URL_ALLOWLIST=hooks.slack.com,open.feishu.cn` for exact-host allowlisting.
- Literal loopback, private, link-local, and other non-global IPs are refused by default.
- For deliberately trusted internal hooks only, opt in separately with `K8S_MCP_NOTIFIER_URL_ALLOW_HTTP=true` and `K8S_MCP_NOTIFIER_ALLOW_PRIVATE_HOSTS=true`.
- HTTP redirects are disabled so an allowlisted initial URL cannot bounce to another host.

## Bootstrap and supply chain

`bootstrap_metrics_server` and `bootstrap_local_path_provisioner` use version-pinned official manifests by default, never moving `latest` or `master` URLs. In production, mirror reviewed manifests internally and override URLs through environment variables.

## Temporary read-only operation

1. Set `K8S_MCP_READ_ONLY=true`.
2. Run `k8s-mcp doctor` to verify the configuration.
3. Use `whoami`, `list_resources`, or `cluster_health_snapshot` for audit and diagnostics.
4. Remove the variable or set it to `false` to return to normal read/write operation.

> [!IMPORTANT]
> Never place cluster-admin credentials, webhook secrets, or bearer tokens in prompts, logs, or committed files.
