# 文档中心

[English](./README.en.md) · [项目首页](../README.md)

这里按“先安全、再连接、最后授权”的顺序组织文档。当前项目公开 **91 个工具**；工具总数、首页和发版检查由 CI 同步校验。

## 新用户路径

1. [快速开始](./quickstart.md)：安装、认证、客户端配置与 `doctor`。
2. [安全模型](./security.md)：按需只读、写入边界、并发限制与 webhook 边界。
3. [部署与 RBAC](./deployment.md)：从只读身份到命名空间受限写入。
4. [工具参考](./tools-reference.md)：完整签名目录。

## 文档地图

| 主题 | 中文 | English |
| --- | --- | --- |
| 上手、认证、MCP 客户端 | [quickstart.md](./quickstart.md) | [quickstart.en.md](./quickstart.en.md) |
| 安全、迁移、运行时守门 | [security.md](./security.md) | [security.en.md](./security.en.md) |
| ServiceAccount 与最小 RBAC | [deployment.md](./deployment.md) | [deployment.en.md](./deployment.en.md) |
| 全部 `K8S_MCP_*` 变量 | [env.md](./env.md) | [env.en.md](./env.en.md) |
| NVIDIA GPU / AI 工作负载 | [gpu.md](./gpu.md) | [gpu.en.md](./gpu.en.md) |
| **91 个工具**完整签名 | [tools-reference.md](./tools-reference.md) | [tools-reference.md](./tools-reference.md) |
| 重点工具与工作流 | [tools.md](./tools.md) | — |
| Python 直接调用 | [usage.md](./usage.md) | — |
| 示例与排障 | [examples.md](./examples.md) / [troubleshooting.md](./troubleshooting.md) | — |
| 架构、发布、变更 | [architecture.md](./architecture.md) / [publishing.md](./publishing.md) | — |

> [!NOTE]
> [PLAN.md](./PLAN.md) 是 archived material，可能描述已移除的两阶段删除流程；请以当前 README、安全文档和工具 docstring 为准。
