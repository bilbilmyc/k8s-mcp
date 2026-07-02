# k8s-mcp

[English version](./README.en.md)

面向 LLM Agent 的 Kubernetes MCP server。提供 30+ 工具，覆盖 Pod /
Deployment / StatefulSet / DaemonSet / Service / Ingress / ConfigMap
等资源的增删改查，加上日志 / 事件 / 节点运维 / top / rollout / wait /
批量 YAML apply。

设计目标：让日常 K8s 运维通过自然语言驱动（Claude Desktop、Cursor、
Cline、Cherry Studio…），用结构化 tool 调用替代 `kubectl` 文本解析。

## 快速开始

```bash
# 从源码（开发模式）
uv sync
uv run k8s-mcp

# 从已构建的 wheel（生产 / Agent 配置）
uv build
uv tool run --from ./dist/k8s_mcp-0.1.0-py3-none-any.whl k8s-mcp
```

就这样。默认读 `~/.kube/config`，通过环境变量可覆盖（见 [环境变量参考](./docs/env.md)）。

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

## Claude Desktop / Cursor / Cline / Cherry Studio / Claude Code 配置

推荐用 `uv tool run --from <wheel>`，所有 Agent 的注册方式一致，与源码
在机器上的位置无关。

```json
{
  "mcpServers": {
    "k8s": {
      "command": "uv",
      "args": [
        "tool", "run", "--from",
        "/Users/mayc/codes/k8s-mcp/dist/k8s_mcp-0.1.0-py3-none-any.whl",
        "k8s-mcp"
      ],
      "env": {
        "K8S_MCP_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

Claude Code 的注册方式：

```bash
claude mcp add-json k8s "$(cat <<'EOF'
{
  "command": "uv",
  "args": ["tool", "run", "--from",
           "/Users/mayc/codes/k8s-mcp/dist/k8s_mcp-0.1.0-py3-none-any.whl",
           "k8s-mcp"],
  "env": { "K8S_MCP_LOG_LEVEL": "INFO" }
}
EOF
)"
```

想用模式 A 就把 `K8S_MCP_API_SERVER` 和 `K8S_MCP_API_TOKEN` 加到 `env`
块里。模式 C 不需要任何 env——它读 pod 自己的 SA token。

重启 Agent，应该看到 "k8s" 下挂着约 46 个工具。

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

详见 [docs/env.md](./docs/env.md)。

## 工具目录（约 50 个）

### 读（始终安全）

- `list_resources(kind, namespace?, label_selector?)` — 列出任意内置 Kind
- `get_resource(kind, name, namespace?)` — 完整 JSON 对象
- `get_resource_yaml(kind, name, namespace?, reveal_secrets=False)` — YAML 清单；Secret 默认脱敏
- `describe_resource(kind, name, namespace?)` — kubectl-describe 风格摘要
- `get_resource_jsonpath(kind, path, name?, namespace?, label_selector?)` — 提取单个字段
- `diff_resource(yaml_content)` — 预览 apply_yaml 会改什么（CREATE vs UPDATE、顶层字段变化）
- `list_pods(namespace?, label_selector?, field_selector?, include_all=False)`
- `list_events(namespace?, field_selector?, warning_only=False, limit=50)`
- `get_pod_logs(pod_name|label_selector, namespace, container?, tail_lines?, since_seconds?, since_time=RFC3339?, until_time=RFC3339?, strict_time=False, previous=False, timestamps=False, pattern=regex?, context_lines=0, max_bytes=1MiB, output_format=text|json)` — 空结果返回中文友好提示，不是空白
- `get_configmap(name, namespace)`
- `list_secrets(namespace?, label_selector?)` — 仅 metadata，绝不返回值
- `get_secret_value(name, namespace, key, reveal=False)` — 单 key 窄爆炸半径读取；`reveal` 必须显式为 True
- `top_pods(namespace?, label_selector?, sort_by=memory|cpu)` — 需要 metrics-server
- `top_nodes(sort_by=memory|cpu)` — 需要 metrics-server
- `prometheus_query(promql, time?, prometheus_url?)` — Prometheus 即时 PromQL 查询（**不是** metrics-server，可以查任意 Prometheus 已抓取的指标）
- `prometheus_query_range(promql, start, end, step="30s", prometheus_url?)` — Prometheus 时间序列范围查询
- `pod_metrics(pod_name, namespace, metric="cpu|memory|network_rx|network_tx|fs_reads|fs_writes", range="5m", prometheus_url?)` — 用 cAdvisor 默认指标名查某个 Pod 的 CPU / 内存 / 网络 / 磁盘读写速率
- `find_prometheus_service(namespace=None)` — 在集群里扫描所有（或指定 namespace）的 Service，找出名字看起来像 Prometheus 的；返回 NAMESPACE/NAME/CLUSTER_IP/PORT/URL 表，Agent 应当挑一个 URL 传给上面三个工具的 `prometheus_url` 参数
- `start_prometheus_port_forward(namespace, service_name, service_port=9090, local_port=None)` — **关键工具**：Prometheus Service 默认是 ClusterIP（`10.96.x.x` 这种虚拟 IP），只能在集群 pod 内部访问。MCP server 跑在你机器上，外面访问不通（TCP RST）。这个工具起一个 `kubectl port-forward` 把集群内端口钓到 `127.0.0.1` 上，返回本地 URL，让 Agent 能正常调上面三个工具
- `list_port_forwards()` / `stop_port_forward(forward_id)` — 查看 / 终止活跃的 port-forward
- `rollout_status(kind, name, namespace, timeout_seconds=60, watch=False)` — 轮询直到 rollout 完成
- `rollout_history(kind, name, namespace)` — 列出 ControllerRevisions；传给 `rollout_undo(to_revision=)`
- `get_api_resources(prefix=None)` — 列出集群所有 kind（含 CRD）
- `explain_resource(kind, field_path?, api_version?)` — 通过 OpenAPI schema 做 `kubectl explain`

### 写（受 read-only 和 namespace-allowlist 限制）

- `apply_yaml(yaml_content)` — 应用单文档或多文档清单
- `replace_resource(yaml_content)` — PUT 带 ResourceVersion；集群看到更新版本则拒绝
- `create_deployment(name, image, namespace?, replicas?, container_name?, ports?, env?, labels?, resources?, image_pull_policy?)`
- `create_statefulset(name, image, service_name, namespace?, replicas?, ...)`
- `create_service(...)`, `create_ingress(...)`, `expose_workload(...)`
- `create_hpa(name, target_kind, target_name, namespace, min_replicas, max_replicas, cpu_utilization?, memory_average_value?)`
- `create_pdb(name, target_kind, target_name, namespace, min_available=... | max_unavailable=...)`
- `create_role(name, namespace, rules)`, `create_rolebinding(name, namespace, role_kind, role_name, subjects)`
- `create_clusterrole(name, rules)`, `create_clusterrolebinding(name, role_name, subjects)`
- `create_serviceaccount(name, namespace, image_pull_secrets=[]?)`
- `create_networkpolicy(name, namespace, pod_selector, policy_types=[Ingress|Egress], ingress=[], egress=[])`
- `create_pvc(name, namespace, size, access_modes?, storage_class?, labels?)`
- `scale_workload(kind, name, namespace, replicas)`
- `restart_workload(kind, name, namespace)`
- `set_image(kind, name, namespace, container, image)`
- `set_resources(kind, name, namespace, container, requests={}, limits={})` — `kubectl set resources` 等价
- `rollout_undo(kind, name, namespace?, to_revision?)`
- `cordon_node(name)`, `uncordon_node(name)` — 节点调度
- `drain_node(name, ignore_daemonsets=False, delete_emptydir_data=False, force=False, grace_period_seconds=-1, timeout_seconds=60)`
- `delete_pod(name, namespace, grace_period_seconds=30)` — 恢复 / 重启原语，绕过二次确认
- `wait_resource(kind, name, namespace?, for_condition=Ready|..., for_jsonpath=expr?, jsonpath_value?, timeout_seconds=60)`
- `update_configmap(name, namespace, data, merge=False)`

### 删除（二次确认）

- `delete_resource(kind, name, namespace?, confirm=False, confirmation_token?, grace_period_seconds=30)`

### 重点工具说明

**`get_pod_logs`** 专为长跑 Pod 设计（数天 / 数周的日志）：

- 默认：`tail_lines=100`，`max_bytes=1 MiB`。
- `pattern=<regex>` + `context_lines=N` 按正则抓 N 行上下文。
- `label_selector=...` 一次拉多个 Pod 的日志（多 Pod 模式每行前缀
  `[pod-name]`）。
- `output_format=json` 返回 `[{pod, container, time, line}]` 列表。
- 硬上限：16 MiB；超过从头部截断，附 `[truncated]` 标记。
- 当容器没有日志输出（写到文件 / 刚启动 / tail_lines 太小）时，工具
  返回**明确的中文提示**，避免 Agent 误以为"没调用"。
- `since_time` / `until_time` 支持 RFC3339 绝对时间窗口（"两点到四点"），
  K8s API 仅支持下界，`until_time` 客户端过滤；`strict_time=True` 丢弃
  没有 RFC3339 时间戳的行。

**`delete_resource`** 强制走两步流程：

1. 调 `delete_resource(kind=..., name=..., namespace=..., confirm=False)`。
2. 工具返回 `{preview_yaml, confirmation_token, expires_in_seconds}`。
3. 把 YAML 给用户看，明确确认。
4. 再调一次，带 `confirm=True` 和 `confirmation_token`。token 里的
   kind/name/namespace/grace_period 必须匹配。

Token 是 HMAC-SHA256 签名（`K8S_MCP_DELETE_TOKEN_SECRET`），默认 5 分钟过期。

**`drain_node`** 镜像 `kubectl drain`：

- 先 cordon，再用 Eviction API 驱逐 Pod（尊重 PDB）。
- DaemonSet 和 emptyDir Pod 默认跳过（与 kubectl 一致）；重跑加
  `ignore_daemonsets=True` / `delete_emptydir_data=True`。
- `force=True` 绕过 PDB（raw delete）。

**Prometheus 工具**（`prometheus_query` / `prometheus_query_range` /
`pod_metrics`）跟 `top_pods` 是**两套独立体系**：

- `top_pods` 走 Kubernetes 聚合层 API `/apis/metrics.k8s.io/...`，**只能
  从 metrics-server 拉数据**。
- Prometheus 工具走 Prometheus 自己的 HTTP API（默认 `:9090`），能查
  Prometheus 已抓取的所有指标（cAdvisor、node-exporter、各应用的
  exporter / ServiceMonitor 都行）。
- 大多数 Prometheus 部署自带 cAdvisor 指标，所以
  `pod_metrics("nginx-7c5b", "default", "cpu")` 这类查询即使没装
  metrics-server 也能用。

**Prometheus 怎么找**：这是一个**协作问题**——不同集群部署方式（operator、
helm、bare manifest）和 namespace 都不一样。k8s-mcp 走"三层发现 + 一层桥接"协议：

1. **Agent 先调用 `find_prometheus_service(namespace=None)`** —— 扫描整个
   集群（或单个 namespace），列出所有名字像 Prometheus 的 Service
   （`prometheus` / `prometheus-operated` /
   `kube-prometheus-stack-prometheus` / `prometheus-server` 等），给出
   NAMESPACE / NAME / CLUSTER_IP / PORT / URL 的清单。
2. **Agent 看到 ClusterIP 就知道要走 port-forward** —— `find_prometheus_service`
   返回的 URL 是 ClusterIP（`http://10.96.x.x:9090`），这个 IP **从集群外不可达**
   （MCP server 跑在你机器上，访问 10.x 在路由层就 RST）。Agent 必须调
   `start_prometheus_port_forward(namespace, service_name)` 起一个本地端口桥接，
   拿到 `http://127.0.0.1:<auto>` 这种 URL。
3. **Agent 拿本地 URL 调 Prometheus 工具** —— `prometheus_query(promql,
   prometheus_url=<那个本地 URL>)` / `prometheus_query_range(...)` /
   `pod_metrics(..., prometheus_url=<本地 URL>)`。

如果你的 Prometheus Service 已经改成 `NodePort` / `LoadBalancer` /
ExternalService 类型（外部可路由），Agent 可以跳过 step 2，直接用
`find_prometheus_service` 给出的 URL。如果 `K8S_MCP_PROMETHEUS_URL` 已经
设了，工具会直接用它，跳过发现。否则有一个"小候选名单"兜底
（`monitoring/kube-prometheus-stack-prometheus` 等常用组合），兜底失败就
返回中文友好的"问用户"提示。

**port-forward 的生命周期**：subprocess 在 MCP server 重启时会被自动关掉
（`atexit` hook），需要重新调 `start_prometheus_port_forward`。
MCP server 在 Cherry Studio 里被 client 重启时这些 forward 全部失效，
所以"哪个 svc 是 Prometheus"是 Agent 启动后第一件要发现的事。

## 端到端示例（Claude 会话）

> 你："部署 nginx 1.25，Deployment 3 副本，再加 Service 和 Ingress 暴露。"
>
> Claude → `create_deployment`, `expose_workload`, `create_ingress`。
>
> 你："找出最近一小时所有 5xx 错误。"
>
> Claude → `get_pod_logs(label_selector=app=nginx, pattern=r"\b5\d\d\b",
> context_lines=2, since_seconds=3600)`。
>
> 你："给我看看 HPA 的当前副本数。"
>
> Claude → `get_resource_jsonpath("HorizontalPodAutoscaler",
> "status.currentMetrics", name="web", namespace="default")`。
>
> 你："等 Deployment rollout 完成，然后把镜像升到 1.27。"
>
> Claude → `wait_resource("Deployment", "nginx", namespace="default",
> for_condition="Available")` → `set_image(...)`。
>
> 你："drain node-3，我要重启它。"
>
> Claude → `cordon_node("node-3")` → 列 Pod → `drain_node("node-3")`。
>
> 你："看一下 api-1 现在的 CPU 和内存。"
>
> Claude → `find_prometheus_service()` →
> 发现 ClusterIP 不可达 → `start_prometheus_port_forward("default",
> "monitor-kube-prometheus-st-prometheus")` 拿到 `http://127.0.0.1:34567` →
> `pod_metrics("api-1", "default", "cpu", prometheus_url="http://127.0.0.1:34567")` →
> `pod_metrics("api-1", "default", "memory", prometheus_url="http://127.0.0.1:34567")`。
>
> 你："把它删了。"
>
> Claude → `delete_resource(confirm=False)` → 给你看 YAML 预览。
>
> 你："好，删吧。"
>
> Claude → `delete_resource(confirm=True, confirmation_token=...)`。

## 开发

```bash
uv sync
uv run pytest              # 182 个测试
uv run ruff check .        # lint
uv run k8s-mcp             # stdio 启动
uv build                   # 生成 dist/*.whl + .tar.gz
```

## 架构

```
src/k8s_mcp/
├── server.py         # FastMCP 入口，注册所有工具
├── config.py         # Settings（pydantic-settings，K8S_MCP_* env）
├── auth.py           # 三档认证（apiserver+token / kubeconfig / in-cluster）
├── client.py         # 缓存的 ApiClient 工厂
├── formatters.py     # YAML / Table / Describe + Secret 脱敏
├── safety.py         # HMAC 二次确认 token
└── tools/
    ├── generic.py    # list/get/get_yaml/describe/apply_yaml
    ├── workload.py   # create_deployment/statefulset, scale/restart/set_image
    ├── service.py    # create_service/ingress, expose_workload
    ├── logs.py       # get_pod_logs（长日志优化）
    ├── pods.py       # list_pods
    ├── events.py     # list_events
    ├── configmap.py  # get/update_configmap
    ├── delete_tool.py# delete_resource（两步确认）
    ├── metrics.py    # top_pods / top_nodes
    ├── rollout.py    # rollout_status / rollout_undo / rollout_history
    ├── node_ops.py   # cordon / uncordon / drain
    ├── wait_tool.py  # wait_resource（condition 或 JSONPath）
    ├── jsonpath.py   # get_resource_jsonpath
    ├── secret.py     # list_secrets + get_secret_value（单 key）
    ├── discovery.py  # get_api_resources + explain_resource
    ├── autoscale.py  # create_hpa + create_pdb
    ├── rbac.py       # Role / RoleBinding / ClusterRole / ClusterRoleBinding
    ├── serviceaccount.py # create_serviceaccount
    ├── networkpolicy.py # create_networkpolicy
    ├── storage.py    # create_pvc
    └── prometheus.py # prometheus_query / prometheus_query_range / pod_metrics
```

`generic.py` 还额外暴露 `replace_resource`（PUT 带 ResourceVersion）和
`diff_resource`（apply 前预览差异）。

完整设计文档见 [PLAN.md](./PLAN.md)，用法示例见 [tests/](./tests/)。

## 程序化调用（无需 MCP）

每个注册到 FastMCP 的工具同时也是 `k8s_mcp.tools.*` 下的纯 Python 函数，
所以你可以在脚本、notebook 或 CLI 里直接调，不用启 MCP server。认证、
安全、namespace allowlist 都仍然生效——它们住在 `config`、`safety` 和
各 tool 的内部检查里，不在 MCP 层。

```python
# 程序化调用示例 —— 直接 import 函数，无需 MCP server
# 1) 加载配置（读取 K8S_MCP_* 环境变量）
from k8s_mcp.config import get_settings, reset_settings_cache
reset_settings_cache()  # 清掉可能的缓存
settings = get_settings()
print(settings.read_only, settings.namespace_allowlist)

# 2) 直接调一个 tool 函数 —— 与 MCP 工具签名完全一致
from k8s_mcp.tools import logs
result = logs.get_pod_logs(
    pod_name="nginx-7c5b-abc",
    namespace="default",
    tail_lines=50,
    pattern=r"\b5\d\d\b",      # 正则：抓 5xx 错误
    context_lines=2,           # 匹配前后各 2 行
    since_seconds=3600,        # 最近一小时
)
print(result)  # 纯文本，可直接进日志/告警

# 3) 时间窗口（绝对时间）—— "两点到四点之间"
from k8s_mcp.tools import logs
out = logs.get_pod_logs(
    pod_name="api-1",
    namespace="prod",
    since_time="2026-07-02T14:00:00Z",   # RFC3339，下界
    until_time="2026-07-02T16:00:00Z",   # RFC3339，上界（客户端过滤）
    pattern="aabbcc",
)

# 4) 创建资源 —— 走和 MCP 一样的守门（read-only / namespace allowlist）
from k8s_mcp.tools import workload
out = workload.create_deployment(
    name="web",
    image="nginx:1.25",
    namespace="default",
    replicas=3,
)
print(out)

# 5) 删除二次确认 —— 与 MCP 流程一致
from k8s_mcp.tools import generic as gen
# 第一步：不带 confirm，先拿到预览 + token
preview = gen.delete_resource(kind="Deployment", name="web", namespace="default")
print(preview)  # 含 confirmation_token
# 第二步：人工确认后，带 confirm=True + token 真正执行
# gen.delete_resource(kind="Deployment", name="web", namespace="default",
#                    confirm=True, confirmation_token="<token-from-preview>")
```

`k8s_mcp.client.get_api_client()` 返回缓存的
`kubernetes.client.api_client.ApiClient`，自动套用同样的三档认证，所以
任何想下沉到原始 kubernetes-python-client 的代码也能享受 kubeconfig /
apiserver-token / in-cluster 自动探测。

## 后续计划（v2+）

- `exec_pod`、`port_forward`（有状态，不适合 MCP stdio）
- 日志流式推送（同上）
- Helm / Kustomize 集成
- 多集群路由
- MCP HTTP / SSE 传输（v1 仅 stdio）
- Docker 镜像 / Helm Chart 发布（v1 只发 wheel）