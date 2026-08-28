# Quick start

[中文](./quickstart.md) · [Documentation](./README.en.md)

## Prerequisites

- Python 3.11+
- A kubeconfig, API-server token, or in-cluster ServiceAccount that can reach the Kubernetes API
- A stdio-capable MCP client

## Install and inspect locally

```bash
pip install k8s-mcp-bilbilmyc
k8s-mcp --help
k8s-mcp doctor
```

| Command | Purpose |
| --- | --- |
| `k8s-mcp` / `k8s-mcp serve` | Run the stdio MCP server |
| `k8s-mcp doctor` | Print redacted runtime policy; does not contact the cluster |
| `k8s-mcp --version` | Print the version |

## Authentication

### kubeconfig (recommended)

```bash
export KUBECONFIG="$HOME/.kube/config"
```

Authentication precedence is: paired API server + token → `K8S_MCP_KUBECONFIG` → in-cluster ServiceAccount → standard `KUBECONFIG` (including merged paths) or `~/.kube/config`. No auth variable is needed for the default path.

### Direct API-server credentials

```bash
export K8S_MCP_API_SERVER=https://api.example.com:6443
export K8S_MCP_API_TOKEN='replace-with-service-account-token'
export K8S_MCP_API_CA_CERT=/absolute/path/to/ca.crt
```

Use `K8S_MCP_API_INSECURE=true` only in controlled environments; it disables TLS certificate verification.

## MCP client configuration

```json
{
  "mcpServers": {
    "k8s": {
      "command": "k8s-mcp",
      "env": {
        "KUBECONFIG": "/absolute/path/to/kubeconfig"
      }
    }
  }
}
```

No argument is the default stdio entry point; `serve` is an optional alias. On Windows, use the absolute executable path from the Python environment that installed `k8s-mcp`. If your client does not inherit shell variables, put required values in its `env` block. Omit the entire block when it can access the default kubeconfig.

## Switch to read-only when needed

```bash
export K8S_MCP_READ_ONLY=true
k8s-mcp doctor
```

For ordinary writes, set `K8S_MCP_NAMESPACE_ALLOWLIST=staging`, then begin with `whoami`, `list_resources`, or `cluster_health_snapshot` to confirm the identity and target namespace. See [Deployment](./deployment.en.md) for RBAC templates.

In other words, common policies need one setting: `K8S_MCP_READ_ONLY=true` for read-only work, or `K8S_MCP_NAMESPACE_ALLOWLIST=staging` for scoped writes. Rate limits, timeouts, concurrency, and the Kubernetes HTTP connection pool already have defaults.

## Verification checklist

- [ ] `doctor` shows the expected `read_only` value and allowlist.
- [ ] The MCP client discovers `ping` and the other tools.
- [ ] The write identity is bound only to namespaces that require it.
- [ ] Production webhooks use an exact host allowlist.
