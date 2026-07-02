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

Prometheus 的 URL 解析有 4 层优先级，**由高到低**：

1. **工具参数 `prometheus_url=`** — Agent 用 `find_prometheus_service()`
   找到的 URL 直接传给 `prometheus_query` / `prometheus_query_range` /
   `pod_metrics`。这是不同集群差异最大的场景的**主要协议**（每个集群
   把 Prometheus 装在不同 namespace / 不同 Service 名）。
2. **环境变量 `K8S_MCP_PROMETHEUS_URL`** — 全局兜底；设了就跳过发现，
   适合 Prometheus 在固定地址的环境。
3. **硬编码小候选名单自动扫描** — `monitoring` /
   `kube-prometheus` / `prometheus` / `observability` 这几个 namespace
   里名为 `kube-prometheus-stack-prometheus` / `prometheus-operated` /
   `prometheus` / `prometheus-server` 的 Service。覆盖大约 80% 的标准
   安装。
4. **找不到** — 工具返回中文友好提示，引导用户给 URL。

Agent 工作流推荐（**两套 ClusterIP 桥接，按场景选**）：

```
find_prometheus_service(namespace=None)
  ↓ 拿到 Service 表（SERVICE + ClusterIP/NodePort）
  ↓
  ├── Service 已是 NodePort/LoadBalancer/External?
  │      ↓ 直接用
  │   prometheus_query(promql, prometheus_url=<它>)
  │
  ├── 节点 IP 可路由到 MCP 客户端？
  │      ↓ 无 kubectl 依赖
  │   expose_prometheus_as_nodeport(namespace, service_name)
  │      ↓ 返回 nodePort 数字
  │   list_resources(kind='Node') 拿节点 IP
  │      ↓
  │   prometheus_query(promql, prometheus_url='http://<node-ip>:<nodePort>')
  │
  └── 节点 IP 不可路由？
         ↓ 依赖 PATH 上的 kubectl
      start_prometheus_port_forward(namespace, service_name)
         ↓ 返回 http://127.0.0.1:<port>
      prometheus_query(promql, prometheus_url='http://127.0.0.1:<port>')
```

> ⚠️ `find_prometheus_service()` 默认返回 ClusterIP（`10.96.x.x`），
> 这种虚拟 IP **只能在集群 pod 内路由**。MCP server 跑在用户机器（Cherry
> Studio 客户端内），从外面访问 10.x 在路由层就 RST。两条桥接路：
> - `expose_prometheus_as_nodeport()` — 创建 K8s 一等公民 NodePort，原
>   ClusterIP Service 保持不动；适合节点 IP 内网可达的环境（VPC /
>   on-prem / dev box）。**nodePort 由 apiserver atomic 分配，不会冲突**。
>   **不依赖任何外部命令**。
> - `start_prometheus_port_forward()` — 起 `kubectl port-forward`，返
>   回 127.0.0.1 URL；适合节点 IP 不可达的环境。**要求 `kubectl` 在 PATH**。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `K8S_MCP_PROMETHEUS_URL` | (空) | 完整 URL，例如 `http://prometheus.monitoring.svc.cluster.local:9090`。设了就跳过自动探测。注意：如果设的是 ClusterIP，从外部访问会失败。 |
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