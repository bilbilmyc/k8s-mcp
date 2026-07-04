# k8s-mcp

[English version](./README.en.md)

面向 LLM Agent 的 Kubernetes MCP server。提供 **70 个**工具，覆盖 Pod /
Deployment / StatefulSet / DaemonSet / Job / CronJob / Service / Ingress
/ ConfigMap / PVC / RBAC / NetworkPolicy 等资源的增删改查，加上日志 /
事件 / 节点运维 / top / rollout / wait / 批量 YAML apply / Prometheus
查询 / 健康巡检 / 主动推送。

设计目标：让日常 K8s 运维通过自然语言驱动（Claude Desktop、Cursor、
Cline、Cherry Studio…），用结构化 tool 调用替代 `kubectl` 文本解析。

> **包名说明**：PyPI 上的名字是 `k8s-mcp-bilbilmyc`（`k8s-mcp` 已被另一个同类
> 项目占用）。`import` 仍是 `k8s_mcp`，CLI 仍是 `k8s-mcp`。详见
> [docs/publishing.md](./docs/publishing.md)。

## 目录

- [安装](#安装)
- [认证 — 三档](#认证-三档)
- [MCP 客户端配置](#mcp-客户端配置)
- [安全守门](#安全守门)
- [通知 webhook](#通知-webhook)
- [文档索引](#文档索引)
- [开发](#开发)

## 安装

```bash
# 1) 装 CLI（一次）
uv tool install k8s-mcp-bilbilmyc

# 2) 验证
k8s-mcp --help
```

**或者一次性跑（不装）**：

```bash
uvx --from k8s-mcp-bilbilmyc k8s-mcp
```

**从源码（开发模式）**：

```bash
git clone https://github.com/bilbilmyc/k8s-mcp
cd k8s-mcp
uv sync
uv run k8s-mcp
```

默认读 `~/.kube/config`，通过环境变量可覆盖（见 [docs/env.md](./docs/env.md)）。

## 认证 — 三档

自动检测，按以下优先级匹配：

### 模式 A — apiserver URL + token

远程 / CI / CD 场景下用，不能用 kubeconfig 时。

```bash
export K8S_MCP_API_SERVER=https://api.example.com:6443
export K8S_MCP_API_TOKEN=eyJhbGciOiJSUzI1NiIs...
export K8S_MCP_API_CA_CERT=/path/to/ca.crt   # 可选
export K8S_MCP_API_INSECURE=false            # 可选，跳过 TLS 校验（仅测试）
```

### 模式 B — kubeconfig

默认。读 `KUBECONFIG` 环境变量或 `~/.kube/config`。

```bash
export KUBECONFIG=/path/to/kubeconfig         # 可选
export K8S_MCP_KUBE_CONTEXT=my-cluster        # 可选，覆盖 current-context
```

### 模式 C — in-cluster

检测到 `/var/run/secrets/kubernetes.io/serviceaccount/token` 时自动启用。
MCP server 作为 sidecar 跑在 pod 内时用。

## MCP 客户端配置

> 推荐用 `uv tool install` 装好后，**所有 Agent 都用同一个 `command: k8s-mcp` 入口**，
> 跟源码在机器上的位置无关，升级也不用改 JSON。

```json
{
  "mcpServers": {
    "k8s": {
      "command": "k8s-mcp",
      "env": {
        "K8S_MCP_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

**Claude Code** 的注册方式：

```bash
claude mcp add-json k8s '{"command": "k8s-mcp", "env": {"K8S_MCP_LOG_LEVEL": "INFO"}}'
```

**想用模式 A** 就把 `K8S_MCP_API_SERVER` 和 `K8S_MCP_API_TOKEN` 加到 `env`
块里。模式 C 不需要任何 env——它读 pod 自己的 SA token。

**还没装？** 把 `command` 改成 `uvx`，临时拉包跑：

```json
{
  "mcpServers": {
    "k8s": {
      "command": "uvx",
      "args": ["--from", "k8s-mcp-bilbilmyc", "k8s-mcp"],
      "env": { "K8S_MCP_LOG_LEVEL": "INFO" }
    }
  }
}
```

重启 Agent，应该看到 "k8s" 下挂着 **70 个**工具。

完整环境变量清单见 [docs/env.md](./docs/env.md)。

## 安全守门

```bash
# 只读模式：所有写工具直接抛 PermissionError
export K8S_MCP_READ_ONLY=true

# 写操作的 namespace 白名单。读不受限制。
# 设置后，cluster-scoped 写入（无 namespace）一律拒绝。
export K8S_MCP_NAMESPACE_ALLOWLIST=default,app,prod

# 删除二次确认 token 的 HMAC 密钥。生产环境务必改！
export K8S_MCP_DELETE_TOKEN_SECRET=$(openssl rand -hex 32)

# token 有效期（秒），默认 300 = 5 分钟
export K8S_MCP_DELETE_TOKEN_TTL_SECONDS=300
```

## 通知 webhook

把 `cluster_health_snapshot` / `get_certificate_expiry` 这类只读结果主动推到 IM：

```bash
export K8S_MCP_NOTIFIERS='[
  {"name": "ops-feishu", "type": "feishu_card",
   "url": "https://open.feishu.cn/open-apis/bot/v2/hook/...",
   "cluster_label": "prod"},
  {"name": "oncall", "type": "slack",
   "url": "https://hooks.slack.com/services/...",
   "cluster_label": "prod"}
]'
```

每条 `{name, type, url, cluster_label?}`。`type` 支持 `feishu`（纯文本） / `feishu_post`（飞书富文本） / **`feishu_card`**（飞书交互卡片 — 生产推荐：header 颜色随 `level` 变化，每个 `## 章节` 渲染成独立 lark_md 块）/ `slack` / `wecom` / `generic`，payload 拼装由 `notify` 工具按 type 处理，不需要 Agent 自己拼。`cluster_label` 加在卡片 header / 消息前缀上，方便一个 webhook 多集群复用。

## 文档索引

**工具相关：**

- [docs/tools-reference.md](./docs/tools-reference.md) — **70 工具完整目录**（每条带签名）
- [docs/tools.md](./docs/tools.md) — 重点工具 deep-dive + 流程（新会话协议 / 删除二次确认 / 批量三步 / Prometheus 桥接）

**配置 / 架构：**

- [docs/env.md](./docs/env.md) — 全部 `K8S_MCP_*` 环境变量
- [docs/architecture.md](./docs/architecture.md) — 源码目录 + 设计要点

**用法 / 示例：**

- [docs/usage.md](./docs/usage.md) — Python 程序化调用（不开 MCP server）
- [docs/examples.md](./docs/examples.md) — 13 个端到端对话片段

**运维：**

- [docs/troubleshooting.md](./docs/troubleshooting.md) — dev 场景踩坑合集
- [docs/publishing.md](./docs/publishing.md) — PyPI 发版流程

**全套目录**：[docs/README.md](./docs/README.md)。

## 开发

```bash
uv sync
uv run pytest              # 416 个测试
uv run ruff check .        # lint
uv run k8s-mcp             # stdio 启动
uv build                   # 生成 dist/*.whl + .tar.gz
```

发版流程见 [docs/publishing.md](./docs/publishing.md)。完整设计文档见
[PLAN.md](./PLAN.md)。

## 后续计划（v2+）

- `exec_pod`（有状态，不适合 MCP stdio）
- 日志流式推送（同上）
- Helm / Kustomize 集成
- 多集群路由
- MCP HTTP / SSE 传输（v1 仅 stdio）
- Docker 镜像 / Helm Chart 发布
- CI + PyPI Trusted Publishing（v1 人工发版）