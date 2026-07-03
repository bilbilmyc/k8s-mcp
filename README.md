# k8s-mcp

[English version](./README.en.md)

面向 LLM Agent 的 Kubernetes MCP server。提供 **74 个**工具，覆盖 Pod /
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
- [认证 — 三档](#认证--三档)
- [MCP 客户端配置](#mcp-客户端配置)
- [安全守门](#安全守门)
- [通知 webhook](#通知-webhook)
- [工具目录（74 个）](#工具目录74-个)
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

重启 Agent，应该看到 "k8s" 下挂着 **74 个**工具。

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
  {"name": "ops-feishu", "type": "feishu",
   "url": "https://open.feishu.cn/open-apis/bot/v2/hook/...",
   "cluster_label": "prod"},
  {"name": "oncall", "type": "slack",
   "url": "https://hooks.slack.com/services/...",
   "cluster_label": "prod"}
]'
```

每条 `{name, type, url, cluster_label?}`。`type` 支持 `feishu` / `slack` / `wecom` / `generic`，payload 拼装由 `notify` 工具按 type 处理，不需要 Agent 自己拼。`cluster_label` 加在消息前缀上，方便一个 webhook 多集群复用。

## 工具目录（74 个）

> **新会话开局协议**：前两件事一定是 `cluster_info()` → `whoami(namespace="<目标 ns>")`。
> 一个告诉你 apiserver / 版本 / 计数，一个告诉你这个身份在目标 namespace 里能做啥。
> 详见 [docs/tools.md](./docs/tools.md)。

### 读（始终安全）

**通用查询**：

- `list_resources(kind, namespace?, label_selector?, api_version=None)` — 列出任意 Kind；**支持 CRD**（同名 kind 在多个 group 时**必须**显式传 `api_version`）
- `get_resource(kind, name, namespace?, api_version=None)` — 完整 JSON 对象（CRD 感知）
- `get_resource_yaml(kind, name, namespace?, reveal_secrets=False, api_version=None)` — YAML 清单；Secret 默认脱敏
- `describe_resource(kind, name, namespace?, api_version=None)` — kubectl-describe 风格摘要
- `get_resource_jsonpath(kind, path, name?, namespace?, label_selector?)` — 提取单个字段
- `diff_resource(yaml_content)` — 预览 apply_yaml 会改什么
- `get_api_resources(prefix=None)` — 列出集群所有 kind（含 CRD）
- `explain_resource(kind, field_path?, api_version?)` — `kubectl explain` via OpenAPI

**身份 / 版本 / 计数**：

- `cluster_info()` — apiserver URL / GitVersion / 节点 / Pod 计数
- `whoami(namespace="default")` — 当前身份 + 有效 namespace-scoped 权限

**反向查 / 排障**：

- `find_images(image_substring, namespace?, kinds?)` — 扫所有工作负载找匹配 image
- `get_events_for_object(kind, name, namespace?, limit=50)` — 拉一个对象的所有事件
- `list_pods(namespace?, label_selector?, field_selector?, include_all=False)`
- `list_events(namespace?, field_selector?, warning_only=False, limit=50)`
- `get_pod_logs(pod_name|label_selector, namespace, container?, tail_lines?, since_seconds?, since_time=RFC3339?, until_time=RFC3339?, strict_time=False, previous=False, timestamps=False, pattern=regex?, context_lines=0, max_bytes=1MiB, output_format=text|json)` — 详见 [tools.md → get_pod_logs](./docs/tools.md#get_pod_logs)
- `get_configmap(name, namespace)`
- `list_secrets(namespace?, label_selector?)` — 仅 metadata
- `get_secret_value(name, namespace, key, reveal=False)` — 单 key 窄爆炸半径读取

**指标 / 监控**：

- `top_pods(namespace?, label_selector?, sort_by=memory|cpu)` — metrics-server
- `top_nodes(sort_by=memory|cpu)` — metrics-server
- `prometheus_query(promql, time?, prometheus_url?)` — Prometheus 即时 PromQL
- `prometheus_query_range(promql, start, end, step="30s", prometheus_url?)` — 范围查询
- `pod_metrics(pod_name, namespace, metric="cpu|memory|network_rx|network_tx|fs_reads|fs_writes", range="5m", prometheus_url?)` — cAdvisor 指标
- `find_prometheus_service(namespace=None)` — 扫 Prometheus Service + 字面推荐
- `expose_prometheus_as_nodeport(namespace, service_name, name_suffix="-np")` — ⭐ ClusterIP Prometheus 推荐方案
- `start_prometheus_port_forward(namespace, service_name, service_port=9090, local_port=None)` — 节点 IP 不可达时兜底
- `list_port_forwards()` / `stop_port_forward(forward_id)` — port-forward 管理

**运维**：

- `rollout_status(kind, name, namespace, timeout_seconds=60, watch=False)`
- `rollout_history(kind, name, namespace)` — 传给 `rollout_undo(to_revision=)`
- `get_certificate_expiry()` — kubeconfig / SA bundle / apiserver CA 全部证书过期情况
- `cluster_health_snapshot(namespaces=None, events_minutes=60, restart_threshold=3)` — ⭐ 7 维度集群体检
- `notify(message, level="info", notifier_name=None, title=None)` — 主动推 webhook

### 写（受 read-only 和 namespace-allowlist 限制）

**应用**：

- `apply_yaml(yaml_content)` — 单文档或多文档清单
- `replace_resource(yaml_content)` — PUT 带 ResourceVersion

**Workload**：

- `create_deployment(name, image, namespace?, replicas?, container_name?, ports?, env?, labels?, resources?, image_pull_policy?)`
- `create_statefulset(name, image, service_name, namespace?, replicas?, ...)`
- `create_job(name, image, namespace?, command?, args?, env?, resources?, restart_policy="Never", backoff_limit?)` — 一次性任务
- `create_cronjob(name, image, schedule, namespace?, command?, args?, env?, resources?, restart_policy="OnFailure")` — 定时任务

**Service / Ingress**：

- `create_service(...)`, `create_ingress(...)`, `expose_workload(...)`

**存储**：

- `create_pvc(name, namespace, size, access_modes?, storage_class?, volume_name?, labels?)` — `volume_name` 显式绑 hostPath PV
- `validate_pv_hostpath_paths()` — 列出 hostPath PV + 一键 `ssh` 检查
- `bootstrap_local_path_provisioner(set_as_default=True, apply_immediately=True)` — 一键装 Rancher local-path 给无 SC 的 dev/test 集群

**运维**：

- `scale_workload(kind, name, namespace, replicas)`
- `restart_workload(kind, name, namespace)`
- `set_image(kind, name, namespace, container, image)`
- `set_resources(kind, name, namespace, container, requests={}, limits={})`
- `bulk_set_image / bulk_restart / bulk_scale(label_selector, ..., dry_run=True, confirm=False, confirmation_token?)` — 三步安全流程
- `rollout_undo(kind, name, namespace?, to_revision?)`
- `wait_resource(kind, name, namespace?, for_condition=..., for_jsonpath=expr?, jsonpath_value?, timeout_seconds=60)`
- `update_configmap(name, namespace, data, merge=False)`
- `cordon_node(name)`, `uncordon_node(name)` — 节点调度
- `drain_node(name, ignore_daemonsets=False, delete_emptydir_data=False, force=False, grace_period_seconds=-1, timeout_seconds=60)`

**RBAC / NetworkPolicy / ServiceAccount**：

- `create_role / create_rolebinding / create_clusterrole / create_clusterrolebinding`
- `create_serviceaccount(name, namespace, image_pull_secrets=[]?)`
- `create_networkpolicy(name, namespace, pod_selector, policy_types=[Ingress|Egress], ingress=[], egress=[])`
- `create_hpa / create_pdb`

### 删除

按风险 / 确认级别分三组（详见 [docs/tools.md → 删除二次确认](./docs/tools.md#删除二次确认)）。

**通用（两步确认）** — Secret / 级联删除的 Kind：

- `delete_resource(kind, name, namespace?, confirm=False, confirmation_token?, grace_period_seconds=30)`

**一步删除（恢复友好，无级联）** — 可重立的资源：

- `delete_pod(name, namespace, grace_period_seconds=30)` — 恢复 / 重启原语
- `delete_pvc(name, namespace)` — PVC 删了工作负载只 Pending
- `delete_configmap(name, namespace="default")` — CM 删了让 Pod CrashLoopBackOff，但可重建
- `delete_service(name, namespace="default")` — 流量规则不是工作负载
- `delete_ingress(name, namespace="default")` — 外部 HTTP(S) 路由断、Pod 不动

**批量删除（dry-run → token → confirm）** — 一次影响一批：

- `bulk_delete_pvc(label_selector, namespace=None, dry_run=True, confirm=False, confirmation_token?)` — 专清孤儿 PVC

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [docs/env.md](./docs/env.md) | 全部 `K8S_MCP_*` 环境变量参考 |
| [docs/tools.md](./docs/tools.md) | 工具详细使用说明（新会话协议、批量三步、Prometheus 桥接……） |
| [docs/troubleshooting.md](./docs/troubleshooting.md) | dev 场景踩坑（无 SC、hostPath、Forbidden、Prometheus 找不到） |
| [docs/examples.md](./docs/examples.md) | 13 个端到端 Claude / Cherry Studio 对话片段 |
| [docs/architecture.md](./docs/architecture.md) | 源码目录结构 + 设计要点 |
| [docs/usage.md](./docs/usage.md) | Python 程序化调用（不开 MCP server） |
| [docs/publishing.md](./docs/publishing.md) | PyPI / TestPyPI 发版流程（token 生成、uv publish、验证） |

## 开发

```bash
uv sync
uv run pytest              # 419 个测试
uv run ruff check .        # lint
uv run k8s-mcp             # stdio 启动
uv build                   # 生成 dist/*.whl + .tar.gz
```

发版流程见 [docs/publishing.md](./docs/publishing.md)。完整设计文档见
[PLAN.md](./PLAN.md)。

## 后续计划（v2+）

- `exec_pod`、`port_forward`（有状态，不适合 MCP stdio）
- 日志流式推送（同上）
- Helm / Kustomize 集成
- 多集群路由
- MCP HTTP / SSE 传输（v1 仅 stdio）
- Docker 镜像 / Helm Chart 发布
- CI + PyPI Trusted Publishing（v1 人工发版）
