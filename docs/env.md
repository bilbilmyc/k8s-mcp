# 环境变量参考

k8s-mcp 通过 pydantic-settings 读取环境变量，所有变量以 `K8S_MCP_`
为前缀。变量大小写不敏感，未设置时使用代码里的默认值。

## 三种使用方式

按使用频率排序：

1. **MCP JSON 配置里 `env` 块** — Claude Desktop / Cursor / Cherry Studio
   等 Agent 启动 MCP server 时直接传，**最常用**。
2. **shell 里 `export`** — 本地调试 MCP server 时方便。
3. **项目根目录的 `.env` 文件** — `pydantic-settings` 自动读取（文件名
   不能改，必须是 `.env`），git 已 ignore；只在你用 `uv run k8s-mcp`
   直接启动时生效。

## 全部变量

### 日志 / 输出

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `K8S_MCP_LOG_LEVEL` | `INFO` | 标准 logging 级别：`DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `K8S_MCP_DEFAULT_TAIL_LINES` | `100` | `get_pod_logs` 不传 `tail_lines` 时的默认行数 |

### 认证 — 模式 A：apiserver URL + token

`K8S_MCP_API_SERVER` 与 `K8S_MCP_API_TOKEN` **必须同时设置**才会启用。

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `K8S_MCP_API_SERVER` | ✓ | apiserver 地址，例如 `https://api.example.com:6443` |
| `K8S_MCP_API_TOKEN` | ✓ | bearer token，对应一个能访问集群的 ServiceAccount |
| `K8S_MCP_API_CA_CERT` |   | apiserver CA 证书路径；不传则用系统 CA bundle |
| `K8S_MCP_API_INSECURE` |   | `true` 时跳过 TLS 校验（**仅测试环境用**） |

### 认证 — 模式 B：kubeconfig

| 变量 | 说明 |
| --- | --- |
| `K8S_MCP_KUBECONFIG` | kubeconfig 文件绝对路径；不传则读 `KUBECONFIG` 环境变量，再读 `~/.kube/config` |
| `K8S_MCP_KUBE_CONTEXT` | 覆盖 kubeconfig 里的 `current-context` |

### 认证 — 模式 C：in-cluster

无需任何环境变量。检测到 `/var/run/secrets/kubernetes.io/serviceaccount/token`
时自动启用（MCP server 作为 sidecar 跑在 pod 内时）。

### 安全守门

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `K8S_MCP_READ_ONLY` | `false` | `true` 时所有写工具（apply / create / patch / delete）拒绝并抛 `PermissionError` |
| `K8S_MCP_NAMESPACE_ALLOWLIST` | (空) | 逗号分隔的 namespace 白名单。设置后，**仅这些 namespace 允许写**；cluster-scoped 资源（无 namespace）的写入也会被拒。读取不受影响。 |
| `K8S_MCP_DELETE_TOKEN_SECRET` | `change-me` | 删除二次确认 token 的 HMAC 签名密钥。**生产环境务必用 `openssl rand -hex 32` 重新生成**。 |
| `K8S_MCP_DELETE_TOKEN_TTL_SECONDS` | `300` | token 有效期（秒），默认 5 分钟 |

### Prometheus（可选，监控查询）

未设置时自动在 `monitoring` / `prometheus` / `kube-prometheus` /
`observability` 这几个 namespace 找名为 `prometheus` /
`prometheus-operated` / `kube-prometheus-stack-prometheus` /
`prometheus-server` 的 Service。找不到时工具会返回"问用户"的提示。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `K8S_MCP_PROMETHEUS_URL` | (空) | 完整 URL，例如 `http://prometheus.monitoring.svc.cluster.local:9090`。设了就跳过自动探测。 |
| `K8S_MCP_PROMETHEUS_BEARER_TOKEN` | (空) | 可选 bearer token。多数本地 Prometheus 不需要。 |

## 完整示例（`~/.zshrc` 或 `.env`）

```bash
# 日志
export K8S_MCP_LOG_LEVEL=INFO
export K8S_MCP_DEFAULT_TAIL_LINES=200

# 模式 A：直连 apiserver
export K8S_MCP_API_SERVER=https://api.prod.example.com:6443
export K8S_MCP_API_TOKEN=$(kubectl -n kube-system get secret admin-token -o jsonpath='{.data.token}' | base64 -d)
export K8S_MCP_API_CA_CERT=/etc/k8s/ca.crt

# 安全
export K8S_MCP_READ_ONLY=false
export K8S_MCP_NAMESPACE_ALLOWLIST=default,app,prod
export K8S_MCP_DELETE_TOKEN_SECRET=$(openssl rand -hex 32)
export K8S_MCP_DELETE_TOKEN_TTL_SECONDS=300
```

## MCP JSON 配置里的 env 块示例

```json
{
  "mcpServers": {
    "k8s": {
      "command": "uv",
      "args": ["tool", "run", "--from",
               "/Users/mayc/codes/k8s-mcp/dist/k8s_mcp-0.1.0-py3-none-any.whl",
               "k8s-mcp"],
      "env": {
        "K8S_MCP_LOG_LEVEL": "INFO",
        "K8S_MCP_API_SERVER": "https://api.example.com:6443",
        "K8S_MCP_API_TOKEN": "eyJhbGciOiJSUzI1NiIs...",
        "K8S_MCP_READ_ONLY": "false",
        "K8S_MCP_NAMESPACE_ALLOWLIST": "default,app,prod",
        "K8S_MCP_DELETE_TOKEN_SECRET": "<32字节hex>"
      }
    }
  }
}
```