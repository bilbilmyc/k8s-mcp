# k8s-mcp

[English version](./README.en.md)

面向 LLM Agent 的 Kubernetes MCP server。提供 **74 个**工具，覆盖 Pod /
Deployment / StatefulSet / DaemonSet / Job / CronJob / Service / Ingress
/ ConfigMap / PVC / RBAC / NetworkPolicy 等资源的增删改查，加上日志 /
事件 / 节点运维 / top / rollout / wait / 批量 YAML apply / Prometheus
查询 / 健康巡检 / 主动推送。

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

重启 Agent，应该看到 "k8s" 下挂着约 **74 个**工具。

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

详见 [docs/env.md](./docs/env.md)。

## 工具目录（74 个）

### 读（始终安全）

> **新会话开局协议**：前两件事一定是 `cluster_info()` → `whoami(namespace="<目标 ns>")`。
> 一个告诉你 apiserver 是什么、K8s 什么版本、Pod/Node 数量；一个告诉你
> 这个身份在目标 namespace 里能对哪些资源做什么。前者让 Agent 知道兼容性边界
> （PodDisruptionBudget v1 需 1.21+、IngressClass 需 1.18+ 等），后者让
> `Forbidden` 类错误在写之前就被预测到。

- `list_resources(kind, namespace?, label_selector?, api_version=None)` — 列出任意 Kind；**支持 CRD**（传 `api_version='cert-manager.io/v1'` 等；同名 kind 在多个 group 时**必须**显式传）
- `get_resource(kind, name, namespace?, api_version=None)` — 完整 JSON 对象（支持 CRD）
- `get_resource_yaml(kind, name, namespace?, reveal_secrets=False, api_version=None)` — YAML 清单；Secret 默认脱敏（支持 CRD）
- `describe_resource(kind, name, namespace?, api_version=None)` — kubectl-describe 风格摘要（支持 CRD）
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
- `find_prometheus_service(namespace=None)` — 在集群里扫描所有（或指定 namespace）的 Service，找出名字看起来像 Prometheus 的；返回 **NAMESPACE / NAME / TYPE / RECOMMENDED / URL** 表。`TYPE=ClusterIP` 行的 `RECOMMENDED` 列直接给出 `expose_prometheus_as_nodeport(namespace='<ns>', service_name='<name>')` 的字面调用签名——Agent **按字面照抄** 调一次即可拿到 nodePort，不再绕弯
- `expose_prometheus_as_nodeport(namespace, service_name, name_suffix="-np")` — ⭐ **ClusterIP Prometheus 推荐方案**：**不依赖 kubectl**，创建一个**平行的 NodePort Service**（名字 `<原>-np`，selector / labels 跟原 Service 完全一样；只克隆 `name=http|web|prometheus` 那个端口，避免浪费 NodePort 槽位）。原 ClusterIP Service **不动**——集群内调用继续走它，新 NodePort 给集群外节点可达时用。**nodePort 由 K8s apiserver 自己分配**（atomic、不会冲突）。**适用场景**：集群节点 IP 可以从 MCP 客户端路由到（典型：VPC 内网 / 公司内网 / 同网段 dev box / minikube / kind）
- `start_prometheus_port_forward(namespace, service_name, service_port=9090, local_port=None)` — **kubectl 桥接兜底方案**：Prometheus Service 默认是 ClusterIP（`10.96.x.x` 这种虚拟 IP），只能在集群 pod 内部访问。MCP server 跑在你机器上，外面访问不通（TCP RST）。这个工具起一个 `kubectl port-forward` 把集群内端口钓到 `127.0.0.1` 上，返回本地 URL，让 Agent 能正常调上面三个工具。**前提：PATH 上有 `kubectl`**。**仅在 NodePort 走不通时使用**：依赖外部二进制、macOS 沙箱下偶尔出 IPv6 绑定问题（`[Errno 61] Connection refused`）
- `list_port_forwards()` / `stop_port_forward(forward_id)` — 查看 / 终止活跃的 port-forward
- `rollout_status(kind, name, namespace, timeout_seconds=60, watch=False)` — 轮询直到 rollout 完成
- `rollout_history(kind, name, namespace)` — 列出 ControllerRevisions；传给 `rollout_undo(to_revision=)`
- `get_api_resources(prefix=None)` — 列出集群所有 kind（含 CRD）
- `explain_resource(kind, field_path?, api_version?)` — 通过 OpenAPI schema 做 `kubectl explain`
- `get_certificate_expiry()` — 聚合查证书过期时间。**apiserver 自己的 serving cert 无法通过 K8s API 查**，但 MCP server 看得见的 4 个源都打包读：`K8S_MCP_API_CA_CERT` / in-cluster SA bundle / kubeconfig CA / kubeconfig client cert（最后那个仅当 kubeconfig 用证书认证时存在）。每个证书给出 Subject / Issuer / NotBefore / NotAfter / 剩余天数 / 状态（✅ valid / ⚠️<30d / ❌<7d / ❌EXPIRED），按天数升序排，最近的过期先显示，并自动追加 "Action needed" 段落提醒。**不靠 apiserver 也不发请求**——纯本地解析。
- `cluster_health_snapshot(namespaces=None, events_minutes=60, restart_threshold=3)` — ⭐ **AI 运维的入口工具**：一次调用返回 7 维度的集群体检报告（Nodes / Pending Pods / 异常重启 / HPA / Orphan PVs / 证书 / 最近告警事件），顶部带 `✅ HEALTHY` / `⚠️ ATTENTION` 一行汇总。**每节独立容错**，单节 apiserver 报错不会让整份报告空白。被问「集群现在怎么样？」时一次调这个就够；要钻细节再用 `describe_resource` / `get_pod_logs` 跟进。
- `notify(message, level="info", notifier_name=None, title=None)` — 📤 **主动推送**：把任意消息（典型用法是把 `cluster_health_snapshot` 的输出）POST 到一个或多个 webhook。**webhook 列表走 env 配置**（`K8S_MCP_NOTIFIERS='[{name, type, url, cluster_label?}, ...]'`），type 支持 `feishu` / `slack` / `wecom` / `generic`，payload 拼装工具内部按 type 处理。返回每条 webhook 的 `✅/❌` 结果 + 错误细节，失败不抛异常（webhook 死了不能把主流程拖垮）。`notifier_name` 指定只发给某个；不指定就 broadcast。
- `cluster_info()` — ℹ️ **身份 + 版本 + 计数**（新会话第一调）：apiserver URL、是否带 bearer token、`GitVersion` / `Platform` / Major.Minor、Nodes / Namespaces / Pods / Services / Deployments 的实时计数。**每节独立容错**——一个 apiserver 查询失败不会让整份报告空白，单独显示 `error: <status> <reason>`。看集群版本一眼判断功能兼容性（v1.21+ / v1.18+ 之类）。
- `whoami(namespace="default")` — 👤 **身份 + 有效权限**：当前 ServiceAccount / User、UID、所属组、然后用 `SelfSubjectRulesReview` 列出在这个 namespace 里能对哪些 apiGroup / resources / verbs 做什么。写工具返回 `Forbidden` 时，**先调这个**就能知道是 SA 权限不够、还是 namespace 选错，省一轮试错。集群级 ClusterRole 不在这里，单独用 `get_role_bindings` 看。
- `find_images(image_substring, namespace=None, kinds=None)` — 🔍 **反向查镜像**："哪些工作负载还在用 `nginx:1.21`？"或"哪些引用了 `registry.internal/library/`？"——一次调完成 `list_resources` + N × `get_resource_yaml` 的工作。默认扫 Deployment / StatefulSet / DaemonSet 的 containers 和 initContainers，**case-insensitive 子串匹配**，返回 KIND / NAMESPACE / NAME / CONTAINER / IMAGE 表，init container 行加 `[init]` 前缀。
- `get_events_for_object(kind, name, namespace=None, limit=50)` — 📜 **对象范围事件**：用 `field_selector=involvedObject.kind=...,involvedObject.name=...` 一次拉完这个对象的所有 Event，按 `lastTimestamp` 倒序排。被问"为什么 X 挂？"时一次调就够，不用扫整个 namespace 的事件流再人脑 grep。无事件时返回 `(no events for Pod/web-1 in namespace app)` 而不是空表，避免 Agent 把"没数据"误读成"工具挂了"。

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
- `create_pvc(name, namespace, size, access_modes?, storage_class?, volume_name?, labels?)` — `volume_name` 用于把 PVC 显式绑到指定 PV（hostPath / local 的本地卷场景）
- `validate_pv_hostpath_paths()` — 列出全部 hostPath PV + 对应节点 + 一键 `ssh` 检查 / 创建命令（见下方"排错与开发场景"）
- `bootstrap_local_path_provisioner(set_as_default=True, apply_immediately=True)` — 一键给无 SC 的 dev/test 集群装 Rancher local-path provisioner(见下方"排错与开发场景")
- `create_job(name, image, namespace="default", command?, args?, env?, resources?, image_pull_policy?, restart_policy="Never", backoff_limit?, labels?)` — 一次性 Job（DB 迁移、ad-hoc 批处理、一次性脚本）；等价 `kubectl create job`，`restart_policy` 默认 Never（Job Pod 几乎不该 Always）
- `create_cronjob(name, image, schedule, namespace="default", command?, args?, env?, resources?, image_pull_policy?, restart_policy="OnFailure", labels?)` — 定时 Job（夜间备份、周期清理、每 N 分钟同步），`schedule` 接受标准 5 段 cron 表达式（`0 2 * * *` / `*/15 * * * *` 等）
- `scale_workload(kind, name, namespace, replicas)`
- `restart_workload(kind, name, namespace)`
- `set_image(kind, name, namespace, container, image)`
- `set_resources(kind, name, namespace, container, requests={}, limits={})` — `kubectl set resources` 等价
- `bulk_set_image(label_selector, container, image, kind=Deployment, namespace?, dry_run=True, confirm=False, confirmation_token?)` — ⚠️ 批量改 image，走 dry-run → token → confirm 三步
- `bulk_restart(label_selector, kind=Deployment, namespace?, dry_run=True, confirm=False, confirmation_token?)` — ⚠️ 批量 rolling restart（stamp `kubectl.kubernetes.io/restartedAt` 注解）
- `bulk_scale(label_selector, replicas, kind=Deployment, namespace?, dry_run=True, confirm=False, confirmation_token?)` — ⚠️ 批量改 replicas（Deployment / StatefulSet；DaemonSet 无此概念会拒绝）
- `rollout_undo(kind, name, namespace?, to_revision?)`
- `cordon_node(name)`, `uncordon_node(name)` — 节点调度
- `drain_node(name, ignore_daemonsets=False, delete_emptydir_data=False, force=False, grace_period_seconds=-1, timeout_seconds=60)`
- `wait_resource(kind, name, namespace?, for_condition=Ready|..., for_jsonpath=expr?, jsonpath_value?, timeout_seconds=60)`
- `update_configmap(name, namespace, data, merge=False)`

### 删除

按"风险 / 确认级别"分三组。

**通用（二次确认）—— Secret / 任何会级联删资源的 Kind 都走这条：**

- `delete_resource(kind, name, namespace?, confirm=False, confirmation_token?, grace_period_seconds=30)` — `confirm=False` 返回 YAML 预览 + HMAC token；用户确认后再用 `confirm=True` + token 真正删

**一步删除（恢复友好，无级联）—— 可重立的资源，不强制二次确认：**

- `delete_pod(name, namespace, grace_period_seconds=30)` — 恢复 / 重启原语（Pod 删了 Deployment 会拉新的）
- `delete_pvc(name, namespace)` — PVC 删了工作负载只是 Pending 等待重新绑定；同 name 重建即恢复
- `delete_configmap(name, namespace="default")` — CM 是松耦合配置数据，删了会让 Pod 起不来（CrashLoopBackOff），但 CM 可重建
- `delete_service(name, namespace="default")` — Service 是流量路由规则不是工作负载，删了 Pod 继续跑、只是 inbound 断流
- `delete_ingress(name, namespace="default")` — 同上，外部 HTTP(S) 路由断、Pod / Service 不动

**批量删除（dry-run → token → confirm）—— 一次影响一批：**

- `bulk_delete_pvc(label_selector, namespace=None, dry_run=True, confirm=False, confirmation_token?)` — 走与 `bulk_set_image` 一样的三步流程，专清孤儿 PVC（典型场景：StatefulSet 删了但 PVC 留下来了）

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

**CRD 读取**：内置 `list_resources` / `get_resource` / `get_resource_yaml`
/ `describe_resource` 都接受 `api_version` 参数。不传时按硬编码字典
快路径（Pod / Deployment 等）解析；找不到就 fallback 到 DynamicClient
扫所有 API group 找唯一匹配。**什么时候必须显式传**：
  - kind 名在两个 group 里都有（极少见，但 `Deployment` 被重写过的话会撞）→ 错误信息会列出所有候选
  - Agent 想百分百确定是某个 CRD（避免任何一个 layer 误解析）→ 在
    `get_api_resources()` 输出里复制 `apiVersion` 字段直接传
**什么时候不用传**：内置 kind + 标准 CRD 唯一匹配。Agent 拿到报错
`Ambiguous kind 'X' — found in: [...]` 后再加。

**`get_certificate_expiry`** 一次性给 Agent 当前 MCP server 看见的全部
证书的过期情况：

- 解析 4 个源（哪个有就填哪个，都空就告诉你"啥都没看见"+ 排查提示）：
  1. `K8S_MCP_API_CA_CERT`（模式 A 显式 CA）
  2. in-cluster SA bundle（`/var/run/secrets/kubernetes.io/serviceaccount/ca.crt`，模式 C）
  3. kubeconfig 当前的 `clusters[].cluster.certificate-authority-data`（模式 B）
  4. kubeconfig 当前的 `users[].user.client-certificate-data`（仅当 kubeconfig 用证书认证）
- 输出表 `SOURCE / SUBJECT / ISSUER / NOT_BEFORE / NOT_AFTER / DAYS_LEFT / STATUS`，
  按 `DAYS_LEFT` 升序排——最快的过期先露出来。
- 状态：`✅ valid` / `⚠️ expires in N d (<30d)` / `❌ <7d` / `❌ EXPIRED`。
- 自动追加 `Action needed:` 段落高亮非 `✅ valid` 的行。
- **K8s apiserver 自己的 serving cert 查不到**——apiserver 不会通过 API 暴露
  自己证书的 notAfter；想查那个需要 SSH 进 master 节点看 `/etc/kubernetes/pki/apiserver.crt`。
  这块的功能是让 Agent 在对话中**主动**发现过期苗头（"你的 kubeconfig 客户端证书
  还有 14 天过期"），不是替代 OS-level 巡检。

**`delete_resource`** 强制走两步流程：

1. 调 `delete_resource(kind=..., name=..., namespace=..., confirm=False)`。
2. 工具返回 `{preview_yaml, confirmation_token, expires_in_seconds}`。
3. 把 YAML 给用户看，明确确认。
4. 再调一次，带 `confirm=True` 和 `confirmation_token`。token 里的
   kind/name/namespace/grace_period 必须匹配。

Token 是 HMAC-SHA256 签名（`K8S_MCP_DELETE_TOKEN_SECRET`），默认 5 分钟过期。

**`bulk_set_image` / `bulk_restart` / `bulk_scale`** 走 **dry-run → token → confirm** 三步安全流程，因为它们能一次影响几十个工作负载：

1. `dry_run=True`（默认）—— 列出所有匹配 `label_selector` 的资源，**当前值 → 目标值**的对比表。**不写**，不发 token。
2. `dry_run=False, confirm=False` —— 同样预览，但额外返回一个 `confirmation_token`（HMAC-SHA256，5 分钟有效）。
3. `dry_run=False, confirm=True, confirmation_token=...` —— 校验 token，**只对预览时匹配的 N 个资源执行**；预览到确认之间新出现的同名 label 资源**不会被误伤**（token 的 `matched_names` 列表是权威范围）。

Token payload 把每个危险参数（image / container / replicas / label_selector / kind / namespace / op）都签名进去，**改任何一项都校验失败**。`bulk_set_image` 的 token 不能拿去 `bulk_scale`，反之亦然。

匹配工作负载类型：
- `bulk_set_image` / `bulk_restart` 支持 Deployment / StatefulSet / DaemonSet
- `bulk_scale` 只支持 Deployment / StatefulSet（DaemonSet 没 replicas 字段）

**`bulk_delete_pvc`** 走**完全一样**的 dry-run → token → confirm 三步流程，
专门清孤儿 PVC。StatefulSet / Stateful workload 删了之后留下的 `app=db`
标签 PVC 是最常见的清理目标。token payload 签名的是
`op` / `label_selector` / `namespace` / `matched_names` 四个字段——预览到
确认之间**新冒出来的同名 label PVC 不会被删**（token 的 `matched_names`
是权威范围）。资源已不在（`404`）会被记为 `SKIPPED (already gone)` 不算
错误。

**`whoami` / `cluster_info`（新会话开局协议）**：

新对话开始时调两次比之后一直 `Forbidden` 试错要省一轮：

- `cluster_info()` 返回 apiserver URL、是否带 bearer、K8s `GitVersion`、
  `Platform`、Nodes / Namespaces / Pods / Services / Deployments 计数。
  看版本一眼判断兼容性边界（`PodDisruptionBudget v1` 需 1.21+、
  `IngressClass` 需 1.18+、Gateway API 是 opt-in 等）。每节独立容错——
  `list_pod_for_all_namespaces` 失败不会让整份报告空白，单独显示
  `error: 403 Forbidden`。
- `whoami(namespace="<目标 ns>")` 先拉 `SelfSubjectReview`（拿到 user /
  UID / groups），再拉 `SelfSubjectRulesReview`（这个身份在目标 namespace
  里能对哪些 apiGroup / resources / verbs 做什么）。**写工具返回
  `Forbidden` 时先调这个**——能直接定位是 SA 权限不够还是 namespace
  选错。ClusterRole 集群级权限**不在这里**（`SelfSubjectRulesReview`
  只看 namespace-scoped 规则），要看那些用
  `get_role_bindings` / `list_resources(kind="ClusterRoleBinding")`。

**`find_images`** 在 `list_resources` + N × `get_resource_yaml` 的组合
场景里替 Agent 节省 N 次往返：

- 一次调扫遍 Deployment / StatefulSet / DaemonSet（`kinds=` 可缩窄）的
  `containers` 和 `initContainers`，做 case-insensitive 子串匹配。
- init container 行加 `[init]` 前缀（`[init] migrate`），避免和主容器
  同名时的歧义。
- 没匹配时返回 `(no workloads reference an image matching 'xxx')`——
  友好提示而不是空表。
- 适合"哪个工作负载还在用 1.21" / "哪些引用了内部 registry" / "升镜像
  之前先看影响面"。

**`get_events_for_object`** 在 "这个对象最近有什么问题？" 场景里替
Agent 节省一轮全 namespace 事件流扫描：

- 用 `field_selector=involvedObject.kind=...,involvedObject.name=...` 走
  apiserver 端过滤——比 Agent 拉全 namespace events 再 `grep` 快。
- cluster-scoped kind（`Node` / `PersistentVolume`）传 `namespace=None`，
  工具会走 `list_event_for_all_namespaces`。
- 空结果返回 `(no events for Pod/web-1 in namespace app)`——避免 Agent
  把"无事件"误读成"工具坏了"。

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
helm、bare manifest）和 namespace 都不一样。k8s-mcp 走"三层发现 + 两套桥接"协议：

1. **Agent 先调用 `find_prometheus_service(namespace=None)`** —— 扫描整个
   集群（或单个 namespace），列出所有名字像 Prometheus 的 Service
   （`prometheus` / `prometheus-operated` /
   `kube-prometheus-stack-prometheus` / `prometheus-server` 等），给出
   **NAMESPACE / NAME / TYPE / RECOMMENDED / URL** 的清单。**RECOMMENDED
   列直接给出下一步调用的字面签名**——Agent 照抄即可。
2. **Agent 看 TYPE 决定走哪条路**：
   - **`TYPE=NodePort` / `LoadBalancer`**（少见，但生产环境里不少 chart
     默认开 NodePort）→ RECOMMENDED 写 `✅ direct`，URL 模板直接能用，
     跳到 step 3。
   - **`TYPE=ClusterIP`**（默认，绝大多数生产集群）→ RECOMMENDED 列里
     就是 `expose_prometheus_as_nodeport(namespace='<ns>',
     service_name='<name>')` 的**字面签名**。Agent 照抄调用：
     它**创建一个平行的 NodePort Service**（名字 `<svc>-np`），**原
     ClusterIP Service 不动**。我们**故意不传 nodePort**，由 K8s apiserver
     自己从 30000-32767 里 atomic 分配——避免客户端 scan-then-create 的
     TOCTOU race（之前有过：客户端扫完所有已用端口，随机挑一个空闲端口，
     但提交之间被别的客户端先占了，导致 422）。Agent 通过
     `list_resources(kind=Node)` 拿到节点 IP，然后用
     `http://<node-ip>:<node_port>` 去查。**不依赖 kubectl**。
   - **`TYPE=ClusterIP` 但节点 IP 不可路由**（远程 / 多层 NAT / 严格防
     火墙，常见于公有云托管 K8s）→ 退到兜底：`start_prometheus_port_forward(ns, svc)`
     起 kubectl port-forward，拿 `http://127.0.0.1:<local>` 给工具。
     **前提：PATH 上有 `kubectl`**；macOS 沙箱下偶尔会撞 IPv6 绑定
     问题，Agent 看到 `[Errno 61] Connection refused` 时考虑重启 MCP
     或换 NodePort。

3. **Agent 拿 URL 调 Prometheus 工具** —— `prometheus_query(promql,
   prometheus_url=<URL>)` / `prometheus_query_range(...)` /
   `pod_metrics(..., prometheus_url=<URL>)`。

如果 `K8S_MCP_PROMETHEUS_URL` 已经设了，工具会直接用它，跳过发现。
否则有一个"小候选名单"兜底（`monitoring/kube-prometheus-stack-prometheus`
等常用组合），兜底失败就返回中文友好的"问用户"提示。

**两种桥接的对比**：

| 方案 | 推荐度 | 外部依赖 | 是否需要持续进程 | 生命周期 | 谁负责 |
| --- | --- | --- | --- | --- | --- |
| `expose_prometheus_as_nodeport` | ⭐ ClusterIP 默认 | 无 | 否（K8s 原语） | 创建后一直在集群里 | 用 `delete_resource(Service)` 清理 |
| `start_prometheus_port_forward` | 节点 IP 不可达时兜底 | `kubectl` 二进制 | 是（subprocess） | MCP server 重启时自动 kill | 用 `stop_port_forward` 显式清理 |

**`expose_prometheus_as_nodeport` 在 read-only 模式下不可用**——它创建
新 Service，read-only 整个写流程都被拒。`start_prometheus_port_forward`
**也不依赖**读/写权限（它只是起本地端口转发）。两种桥接都要尊重
`K8S_MCP_NAMESPACE_ALLOWLIST`：Service 创建或 port-forward 都校验目标 namespace。

## 排错与开发场景

### 集群没有 StorageClass？dev/test 一键装 local-path

kind / k3s 默认 / minikube（没装 extra）这些场景下，集群**根本没有
StorageClass**，PVC 提交即 Pending。`bootstrap_local_path_provisioner`
一次解决：

```
bootstrap_local_path_provisioner()      # 应用 Rancher local-path-storage
```

装好后 `storage_class_name="local-path"` 立刻可用,PVC 提交即自动
创建 hostPath PV。**生产环境不要用**(hostPath 不抗节点故障)。

参数：
- `set_as_default=True`（默认）—— 把新建的 SC 标为集群默认,后续 PVC 不写
  `storage_class` 也行。
- `apply_immediately=False` —— 只返回 manifest YAML,先看一眼再装(适合审计)。
- `K8S_MCP_LOCAL_PATH_PROVISIONER_URL` —— 离线/内网集群,指向你自家的镜像;
  默认指向 [Rancher 官方 manifest](https://raw.githubusercontent.com/rancher/local-path-provisioner/master/deploy/local-path-storage.yaml)。

Manifest 在 session 内 fetch + cache 一次(每次 MCP 重连会重拉)。

### Pod 一直 FailedMount？hostPath 主机的目录可能没建

dev/test 集群常用手搓的 **hostPath PV**（`spec.hostPath.path=/data/xxx`），
但 kubelet **不会**自动在节点上建这个目录。Pod 卡在 ContainerCreating，
事件里看到：

```
Warning  FailedMount  ... path "/data/k8s/pgsql-sts" does not exist
```

处理流：
1. `validate_pv_hostpath_paths()` —— 列出所有 hostPath PV、对应的节点、
   主机路径，**直接给出一行可复制的 `ssh` 命令**（先 `ls -ld` 检查，
   缺则 `sudo mkdir -p`）。
2. 修好后 Pod 会自动重试挂载。
3. `create_pvc(volume_name="...")` 在绑定的 PV 是 hostPath 时，**返回里
   会自动带 `mkdir -p` 提示**，避免下次再踩坑。

PVC 想绑到具体 hostPath PV 必须显式 `volume_name`（PVC 没有 SC 的情况下，
k8s 不会自动按 hostPath path 匹配）。

## 端到端示例（Claude 会话）

> 你："连上 prod 帮我看看。"（新会话开头）
>
> Claude → `cluster_info()` 拿 apiserver / 版本 / 计数 → `whoami(namespace="prod")` 拿身份和有效权限 → 据此判断能做什么。

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
> 你："还有谁在用 nginx:1.21？我想升级影响面看清楚。"
>
> Claude → `find_images("nginx:1.21")` → 一张表列出所有引用 1.21 的
> Deployment / StatefulSet / DaemonSet 及其容器。
>
> 你："api-1 起来了吗？给我看相关事件。"
>
> Claude → `get_events_for_object(kind="Pod", name="api-1", namespace="prod")`
> → 拿到该 Pod 的所有 Warning / Normal 事件按时间倒序。
>
> 你："跑一个 DB 迁移任务，image 用 postgres:16-alpine，命令 pg_dump。"
>
> Claude → `create_job(name="migrate-2026-07-03", image="postgres:16-alpine",
> namespace="db", command=["pg_dump", "-U", "postgres"], env={"PGHOST": "db"},
> backoff_limit=2)`。
>
> 你："每天凌晨 2 点清一次临时表，搞成定时任务。"
>
> Claude → `create_cronjob(name="tidy-temp", image="alpine:3",
> schedule="0 2 * * *", command=["sh", "-c", "psql ... -c 'TRUNCATE temp_events'"])`。
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
> Claude → `find_prometheus_service()` → RECOMMENDED 列读出
> `expose_prometheus_as_nodeport(namespace='default',
> service_name='monitor-kube-prometheus-st-prometheus')` 照抄调用 →
> 拿到 `node_port=31245` → `list_resources(kind='Node')` 拿节点 IP
> `10.20.30.40` → `pod_metrics("api-1", "default", "cpu",
> prometheus_url="http://10.20.30.40:31245")` →
> `pod_metrics("api-1", "default", "memory",
> prometheus_url="http://10.20.30.40:31245")`。
>
> 你："把 prod namespace 里所有 `app=db` 标签的孤儿 PVC 清掉。"
>
> Claude → `bulk_delete_pvc(label_selector="app=db", namespace="prod")`
> （dry-run，列出来）→ 用户确认 →
> `bulk_delete_pvc(..., confirm=False, dry_run=False)` 拿 token →
> `bulk_delete_pvc(..., confirm=True, confirmation_token=token)` 真删。
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
uv run pytest              # 419 个测试
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
    ├── prometheus.py # prometheus_query / prometheus_query_range / pod_metrics
    ├── certs.py      # get_certificate_expiry（CRD + 内置 kind 都用 DynamicClient）
    ├── health.py     # cluster_health_snapshot（7 维集群体检）
    ├── bulk.py       # bulk_set_image / bulk_restart / bulk_scale
    ├── cluster_info.py # cluster_info（apiserver / 版本 / 计数）
    └── notifier.py   # notify 推送 webhook
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