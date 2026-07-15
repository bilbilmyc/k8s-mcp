# 安全模型

[English](./security.en.md) · [返回文档中心](./README.md)

## 默认策略

| 控制项 | 默认值 | 目的 |
| --- | --- | --- |
| `K8S_MCP_READ_ONLY` | `false` | 设为 `true` 时拒绝写、patch、apply、delete |
| `K8S_MCP_NAMESPACE_ALLOWLIST` | 空 | 写入开启后建议限定目标 namespace |
| `K8S_MCP_RATE_LIMIT_RPM` | `120` | 防止单一工具被失控循环高频调用 |
| `K8S_MCP_TOOL_TIMEOUT_S` | `60` | 限制一次 MCP 请求等待时间 |
| `K8S_MCP_MAX_CONCURRENT_TOOLS` | `8` | 限制同步工具及超时后台请求占用的 worker 数 |
| webhook HTTPS | 强制 | 防止明文泄露消息与凭据 |

## 写入授权

1. 常规模式允许读写与删除。
2. `read_only=true` 时立即拒绝所有写入。
3. 配置 allowlist 时，仅允许其中的 namespace；cluster-scoped 写入会被拒绝。
4. Kubernetes RBAC 仍是最终权限边界；本项目守门不替代 RBAC。

推荐使用独立的只读和写入 kubeconfig，写入 kubeconfig 只绑定目标 namespace 的 Role。模板见[部署指南](./deployment.md)。

## 超时与并发

Kubernetes Python client 的同步网络调用不能被 Python 安全强杀。`tool_timeout_s` 到期后，MCP 会向 Agent 返回可恢复错误，但底层线程可能仍在等待 API Server。该线程继续占用 worker slot，直到实际结束；所有 slot 被占用时，新的调用会快速返回“server is busy”，而不是无限排队。

如存在合法长调用，应结合 API Server/Prometheus 超时谨慎提高超时和并发值，而不是关闭保护。

## Webhook 通知

- 默认仅允许 `https://`；设置 `K8S_MCP_NOTIFIER_URL_ALLOWLIST=hooks.slack.com,open.feishu.cn` 可收紧到精确 host。
- 字面量 loopback、私网、link-local 等非全局 IP 默认拒绝。
- 仅受信任内网 webhook 才分别设置 `K8S_MCP_NOTIFIER_URL_ALLOW_HTTP=true` 和 `K8S_MCP_NOTIFIER_ALLOW_PRIVATE_HOSTS=true`。
- HTTP 请求禁用自动重定向，避免 allowlist 后跳转到其他主机。

## 组件引导与供应链

`bootstrap_metrics_server` 与 `bootstrap_local_path_provisioner` 默认使用固定版本的官方 manifest，而不是 `latest` / `master`。生产环境建议镜像到内部制品库，经评审后通过环境变量覆盖 URL。

## 临时只读运行

1. 设置 `K8S_MCP_READ_ONLY=true`。
2. 运行 `k8s-mcp doctor` 确认配置读取正确。
3. 使用 `whoami`、`list_resources` 或 `cluster_health_snapshot` 完成审计和诊断。
4. 恢复正常读写时删除该变量或设为 `false`。

> [!IMPORTANT]
> 不要把 cluster-admin 凭据、webhook 密钥或 bearer token 放进聊天提示词、日志或提交到仓库。
