# 环境变量参考

[English](./env.en.md) · [返回文档中心](./README.md)

所有变量以 `K8S_MCP_` 为前缀；Pydantic 设置大小写不敏感。建议使用不提交的 `.env` 或客户端 `env` 块注入。

## 运行策略

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `K8S_MCP_LOG_LEVEL` | `INFO` | 日志级别 |
| `K8S_MCP_DEFAULT_TAIL_LINES` | `100` | 日志工具默认尾部行数 |
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

## 可观测性与组件引导

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `K8S_MCP_PROMETHEUS_URL` | 空 | 显式 Prometheus URL，跳过发现 |
| `K8S_MCP_PROMETHEUS_BEARER_TOKEN` | 空 | Prometheus bearer token |
| `K8S_MCP_PROMETHEUS_NAMESPACE_ALLOWLIST` | 空 | 限制 Prometheus 自动发现扫描的 namespace |
| `K8S_MCP_LOCAL_PATH_PROVISIONER_URL` | Rancher `v0.0.32` manifest | 内网/离线时覆盖为审核过的镜像 URL |
| `K8S_MCP_METRICS_SERVER_MANIFEST_URL` | metrics-server `v0.7.2` manifest | 内网/离线时覆盖为审核过的镜像 URL |

## Webhook

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `K8S_MCP_NOTIFIERS` | 空 | JSON 数组：`name`、`type`、`url`、可选 `cluster_label` |
| `K8S_MCP_NOTIFIER_URL_ALLOW_HTTP` | `false` | 仅本地明确需要时允许 HTTP |
| `K8S_MCP_NOTIFIER_URL_ALLOWLIST` | 空 | 逗号分隔精确 host allowlist；生产推荐设置 |
| `K8S_MCP_NOTIFIER_ALLOW_PRIVATE_HOSTS` | `false` | 仅受信任内网 webhook 时允许字面私网 IP |

## 受限写入示例

```bash
export K8S_MCP_READ_ONLY=false
export K8S_MCP_NAMESPACE_ALLOWLIST=staging,preview
export K8S_MCP_RATE_LIMIT_RPM=60
export K8S_MCP_TOOL_TIMEOUT_S=45
export K8S_MCP_MAX_CONCURRENT_TOOLS=4
export K8S_MCP_NOTIFIER_URL_ALLOWLIST=hooks.slack.com,open.feishu.cn
```
