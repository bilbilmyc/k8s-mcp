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

**URL 解析 4 层优先级（由高到低）**：

1. **工具参数 `prometheus_url=`** — Agent 用 `find_prometheus_service()`
   找到的 URL 直接传给 `prometheus_query` / `prometheus_query_range` /
   `pod_metrics`。这是不同集群差异最大的场景的**主要协议**。
2. **`K8S_MCP_PROMETHEUS_URL`** — 全局兜底；设了就跳过发现，
   适合 Prometheus 在固定地址的环境。**注意**：如果设的是 ClusterIP
   （`10.96.x.x`），从外部访问会被 RST。
3. **硬编码小候选名单自动扫描** — `monitoring` /
   `kube-prometheus` / `prometheus` / `observability` 这几个 namespace
   里名为 `kube-prometheus-stack-prometheus` / `prometheus-operated` /
   `prometheus` / `prometheus-server` 的 Service。覆盖大约 80% 的标准
   安装。
4. **找不到** — 工具返回中文友好提示，引导用户给 URL。

Agent 推荐的 ClusterIP 桥接 3 步协议详见
[tools.md → Prometheus 端点发现 + 桥接协议](./tools.md#prometheus-工具prometheus_query-prometheus_query_range-pod_metrics)：

```
find_prometheus_service(namespace=None)
  ↓ 拿到 NAMESPACE / NAME / TYPE / RECOMMENDED / URL 表
  ↓ RECOMMENDED 列字面写明下一步调用，Agent 照抄
  ↓
  ├── TYPE=NodePort / LoadBalancer → 直接用 URL（替换 <node-ip> / <lb-ip>）
  ├── TYPE=ClusterIP, 节点 IP 可路由
  │     → expose_prometheus_as_nodeport(namespace, service_name)
  │     → list_resources(kind="Node") 拿节点 IP
  │     → prometheus_query(promql, prometheus_url='http://<node-ip>:<nodePort>')
  └── TYPE=ClusterIP, 节点 IP 不可路由（公有云托管 K8s 等）
        → 没有兜底；用户自行解决（SSH-tunnel / in-cluster MCP server 模式）
```

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `K8S_MCP_PROMETHEUS_URL` | (空) | 完整 URL，例如 `http://prometheus.monitoring.svc.cluster.local:9090`。设了就跳过自动探测。 |
| `K8S_MCP_PROMETHEUS_BEARER_TOKEN` | (空) | 可选 bearer token。多数本地 Prometheus 不需要。 |

### 通知 webhook

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `K8S_MCP_NOTIFIERS` | (空) | JSON 数组，每条 `{name, type, url, cluster_label?}`。`type` 支持 `feishu` / `feishu_post` / `feishu_card` / `slack` / `wecom` / `generic`，由 `notify` 工具按 type 拼 payload。生产推荐 `feishu_card`：交互卡片 + header 颜色随 `level` 变化 + 每个 `## 章节` 独立渲染。 |

### dev / 离线

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `K8S_MCP_LOCAL_PATH_PROVISIONER_URL` | Rancher 官方 manifest URL | `bootstrap_local_path_provisioner` 离线 / 内网场景下指向自托管镜像的 manifest。 |

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
      "command": "k8s-mcp",
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