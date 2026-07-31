# 环境变量参考

[English](./env.en.md) · [返回文档中心](./README.md)

应用变量以 `K8S_MCP_` 为前缀；标准 `KUBECONFIG` 也受支持。Pydantic 设置大小写不敏感。建议使用不提交的 `.env` 或客户端 `env` 块注入。

## 运行策略

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `K8S_MCP_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |
| `K8S_MCP_DEFAULT_TAIL_LINES` | `100` | 日志工具默认尾部行数，范围 1–10000 |
| `K8S_MCP_READ_ONLY` | `false` | 只读开关；设为 `true` 时拒绝写、patch、apply、delete |
| `K8S_MCP_NAMESPACE_ALLOWLIST` | 空 | 逗号分隔的可写 namespace；生产写入必设 |
| `K8S_MCP_RATE_LIMIT_RPM` | `120` | 每个工具 RPM；`0` 关闭 |
| `K8S_MCP_TOOL_TIMEOUT_S` | `60` | 单次 MCP 请求等待秒数；`0` 关闭 |
| `K8S_MCP_MAX_CONCURRENT_TOOLS` | `8` | 同时运行的同步工具数，范围 1–64 |

## 认证

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `K8S_MCP_API_SERVER` | 空 | 直连 Kubernetes API URL |
| `K8S_MCP_API_TOKEN` | 空 | API bearer token；不要提交 |
| `K8S_MCP_API_CA_CERT` | 空 | CA 证书绝对路径 |
| `K8S_MCP_API_INSECURE` | `false` | 跳过 TLS 验证，仅限受控环境 |
| `K8S_MCP_KUBECONFIG` | 空 | kubeconfig 路径 |
| `K8S_MCP_KUBE_CONTEXT` | 空 | kubeconfig context |
| `KUBECONFIG` | 空 | 标准 kubeconfig 路径或路径列表；优先级低于 in-cluster 和 `K8S_MCP_KUBECONFIG` |

不在集群内且未设置以上变量时，会读取默认 `~/.kube/config`。认证优先级详见[快速开始](./quickstart.md)。

## 可观测性与组件引导

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `K8S_MCP_PROMETHEUS_URL` | 空 | 显式 Prometheus URL，跳过发现 |
| `K8S_MCP_PROMETHEUS_BEARER_TOKEN` | 空 | Prometheus bearer token |
| `K8S_MCP_PROMETHEUS_NAMESPACE_ALLOWLIST` | 空 | 只向这些 namespace 发起 Prometheus Service 列表请求；未设置时一次性扫描全集群 |
| `K8S_MCP_LOCAL_PATH_PROVISIONER_URL` | Rancher `v0.0.32` manifest | 内网/离线时覆盖为审核过的镜像 URL |
| `K8S_MCP_METRICS_SERVER_MANIFEST_URL` | metrics-server `v0.7.2` manifest | 内网/离线时覆盖为审核过的镜像 URL |

`top_pods` / `top_nodes` 不会隐式安装组件；两条读取路径均不可用时只提示显式调用 `bootstrap_metrics_server`。该 manifest 含 cluster-scoped RBAC，设置了 namespace allowlist 的实例会拒绝安装。

## Webhook

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `K8S_MCP_NOTIFIERS` | 空 | JSON 数组：`name`、`type`、`url`、可选 `cluster_label` |
| `K8S_MCP_NOTIFIER_URL_ALLOW_HTTP` | `false` | 仅本地明确需要时允许 HTTP |
| `K8S_MCP_NOTIFIER_URL_ALLOWLIST` | 空 | 逗号分隔精确 host allowlist；生产推荐设置 |
| `K8S_MCP_NOTIFIER_ALLOW_PRIVATE_HOSTS` | `false` | 仅受信任内网 webhook 时允许字面私网 IP |

## 最小策略示例

默认 kubeconfig + 默认读写策略不需要额外变量。只读会话只设置：

```bash
export K8S_MCP_READ_ONLY=true
```

生产受限写入只需增加目标范围：

```bash
export K8S_MCP_NAMESPACE_ALLOWLIST=staging,preview
```

只有基准或容量测试证明默认值不合适时再覆盖性能参数：

```bash
export K8S_MCP_RATE_LIMIT_RPM=60
export K8S_MCP_TOOL_TIMEOUT_S=45
export K8S_MCP_MAX_CONCURRENT_TOOLS=4
```

无需额外配置的传输默认值：Kubernetes HTTP connect/read timeout 为 `5s/30s`；连接池为 `max(8, MAX_CONCURRENT_TOOLS × 2)`；`ApiClient` 与 `DynamicClient` 在进程内复用。`doctor` 会显示这些生效值。
