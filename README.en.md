# k8s-mcp

Kubernetes MCP server for LLM agents. Exposes **70 tools** covering CRUD on
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
- [Authentication — three modes](#authentication-three-modes)
- [MCP client setup](#mcp-client-setup)
- [Safety flags](#safety-flags)
- [Notifier webhooks](#notifier-webhooks)
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

Restart the agent. You should see **70 tools** listed under "k8s".

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
  {"name": "ops-feishu", "type": "feishu_card",
   "url": "https://open.feishu.cn/open-apis/bot/v2/hook/...",
   "cluster_label": "prod"},
  {"name": "oncall", "type": "slack",
   "url": "https://hooks.slack.com/services/...",
   "cluster_label": "prod"}
]'
```

Each entry: `{name, type, url, cluster_label?}`. `type` is `feishu`
(plain text) / `feishu_post` (Feishu rich text) / **`feishu_card`**
(Feishu interactive card — recommended for production: header color
follows `level`, each `## section` block renders as its own `lark_md`
card element) / `slack` / `wecom` / `generic`; the `notify` tool
assembles the per-type JSON payload so the agent doesn't have to.
`cluster_label` is prefixed on the card header / message so a single
webhook can multiplex multiple clusters.

## Documentation index

**Tools:**

- [docs/tools-reference.md](./docs/tools-reference.md) — **Full 70-tool catalog** (one line per tool, full signature)
- [docs/tools.md](./docs/tools.md) — Deep dives + flows (new-session protocol, delete confirmation, bulk 3-step, Prometheus bridge)

**Config / architecture:**

- [docs/env.md](./docs/env.md) — All `K8S_MCP_*` env vars
- [docs/architecture.md](./docs/architecture.md) — Source tree + design notes

**Usage / examples:**

- [docs/usage.md](./docs/usage.md) — Programmatic usage (no MCP server)
- [docs/examples.md](./docs/examples.md) — 13 end-to-end dialogs

**Ops:**

- [docs/troubleshooting.md](./docs/troubleshooting.md) — Dev-cluster gotchas
- [docs/publishing.md](./docs/publishing.md) — PyPI / TestPyPI release workflow

**Full index**: [docs/README.md](./docs/README.md).

## Development

```bash
uv sync
uv run pytest              # 416 tests
uv run ruff check .        # lint
uv run k8s-mcp             # run over stdio
uv build                   # produce dist/*.whl + .tar.gz
```

Release workflow: [docs/publishing.md](./docs/publishing.md). Full design
doc: [PLAN.md](./PLAN.md).

## Out of scope (v2+)

- `exec_pod` (stateful, doesn't fit MCP stdio)
- log streaming (same)
- Helm / Kustomize integration
- Multi-cluster routing
- MCP HTTP / SSE transport (v1 is stdio-only)
- Docker image / Helm chart publishing
- CI + PyPI Trusted Publishing (v1 ships by hand)