# 快速开始

[English](./quickstart.en.md) · [返回文档中心](./README.md)

## 前置条件

- Python 3.11+
- 可访问 Kubernetes API 的 kubeconfig、API Server token 或 in-cluster ServiceAccount
- 支持 stdio MCP 的客户端

## 安装与本地诊断

```bash
pip install k8s-mcp-bilbilmyc
k8s-mcp --help
k8s-mcp doctor
```

| 命令 | 用途 |
| --- | --- |
| `k8s-mcp` / `k8s-mcp serve` | 以 stdio 运行 MCP server |
| `k8s-mcp doctor` | 输出不含密钥的运行策略摘要，不访问集群 |
| `k8s-mcp --version` | 输出版本 |

## 认证

### kubeconfig（推荐）

```bash
export KUBECONFIG="$HOME/.kube/config"
export K8S_MCP_READ_ONLY=false
```

未设置 `KUBECONFIG` 时，Kubernetes Python client 会尝试默认 kubeconfig 和 in-cluster 配置。

### 直连 API Server

```bash
export K8S_MCP_API_SERVER=https://api.example.com:6443
export K8S_MCP_API_TOKEN='replace-with-service-account-token'
export K8S_MCP_API_CA_CERT=/absolute/path/to/ca.crt
```

仅在受控环境中设置 `K8S_MCP_API_INSECURE=true`；它会跳过 TLS 验证。

## MCP 客户端配置

```json
{
  "mcpServers": {
    "k8s": {
      "command": "k8s-mcp",
      "args": ["serve"],
      "env": {
        "K8S_MCP_READ_ONLY": "false",
        "KUBECONFIG": "/absolute/path/to/kubeconfig"
      }
    }
  }
}
```

Windows 请使用已安装 `k8s-mcp` 的 Python 环境中的命令绝对路径；客户端无法继承 shell 环境时，把必要配置放在 `env` 块中。

## 需要时切换为只读

```bash
export K8S_MCP_READ_ONLY=true
k8s-mcp doctor
```

常规写入建议配置 `K8S_MCP_NAMESPACE_ALLOWLIST=staging`，随后先让 Agent 执行 `whoami`、`list_resources` 或 `cluster_health_snapshot`，确认身份与目标 namespace。RBAC 模板见[部署指南](./deployment.md)。

## 验证清单

- [ ] `doctor` 显示预期的 `read_only` 与 allowlist。
- [ ] MCP 客户端已经发现 `ping` 和其他工具。
- [ ] 写入身份只绑定到明确需要的 namespace。
- [ ] 生产 webhook 已设置精确 host allowlist。
