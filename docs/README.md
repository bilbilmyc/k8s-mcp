# Documentation index

所有维护者文档都在 `docs/` 下，按用途分文件。

| 文件 | 内容 |
| --- | --- |
| [tools-reference.md](./tools-reference.md) | **73 个工具完整目录**，每条带签名，按读 / 写 / 删分组 |
| [tools.md](./tools.md) | **重点工具 deep-dive** + 流程（新会话协议 / 删除二次确认 / 批量三步 / Prometheus 桥接） |
| [env.md](./env.md) | 全部 `K8S_MCP_*` 环境变量参考 + Prometheus URL 解析优先级 |
| [architecture.md](./architecture.md) | 源码目录结构 + 设计要点（注册机制 / 守门分层 / 进程内状态 / 测试策略） |
| [usage.md](./usage.md) | Python 程序化调用（不开 MCP server，CI / notebook 场景） |
| [examples.md](./examples.md) | 13 个端到端 Claude / Cherry Studio 对话片段 |
| [troubleshooting.md](./troubleshooting.md) | dev / test 集群踩坑合集（无 SC、hostPath、Forbidden、Prometheus 找不到） |
| [publishing.md](./publishing.md) | PyPI 发版流程（**走 GitHub Actions + OIDC**，`uv publish` 留作应急） |
| [CHANGELOG.md](./CHANGELOG.md) | 全部版本变更记录（SemVer pre-1.0） |
| [ROADMAP.md](./ROADMAP.md) | 当前 Phase 计划 + 完成历史 |
| [PLAN.md](./PLAN.md) | 设计档案（archived，2026-07-05 起的 drift log） |

## 推荐阅读顺序

1. 顶层 [README.md](../README.md) — 安装、认证、MCP 客户端配置、安全守门。
2. 本文件 — 知道遇到问题该翻哪一份。
3. [tools-reference.md](./tools-reference.md) — 看完整的 73 工具签名。
4. [tools.md](./tools.md) — 真用上某工具时翻 deep-dive。
5. [env.md](./env.md) — 调环境变量时翻。
