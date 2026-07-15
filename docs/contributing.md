# 贡献指南

[English](../CONTRIBUTING.md) · [返回文档中心](./README.md)

感谢参与 k8s-mcp。项目优先追求**可审计、可理解的安全运维能力**，而不是无守门地扩展 API 覆盖面。

## 提交 PR 前

1. 重大工具或行为变化先通过 Issue 讨论。
2. 保持安全默认值：写入必须遵守 `read_only` 与 namespace 边界。
3. 每个行为变化都补充或更新单元测试。
4. 同一 PR 内同步中文和英文核心文档。
5. 运行质量检查：

```bash
uv sync --all-extras --dev
uv run ruff check .
uv run pytest -q
uv run python scripts/pre_release_check.py
```

## 工具设计清单

- 名称应以动词开头，参数明确且受约束。
- 返回适合 Agent 上下文窗口的精炼、可行动信息。
- 复用认证、错误脱敏、只读和 namespace 守门。
- 不向 Agent 暴露 token、Kubernetes 原始错误 body、私有地址或 webhook 密钥。

## 文档策略

README、快速开始、安全、部署和环境变量参考均有中英文配对版本。发版检查会校验公开工具数。修改命令、默认值、环境变量或安全行为时必须同步两侧。
