# 工具使用说明

README 只列工具名 + 一句话签名。这一页是**详细使用说明**——每个值得展开的
工具单独一节，写它解决了什么常见坑、参数的语义、为什么这么设计。

> 入口约定：所有 `function_name(...)` 形式都是从 MCP 工具签名直接抄过来的；
> Agent 在对话里看到的工具描述跟这里一致。

## 新会话开局协议

新对话开始时**前两件事**一定是 `cluster_info()` → `whoami(namespace="<目标 ns>")`。
一个告诉你 apiserver 是什么、K8s 什么版本、Pod/Node 数量；一个告诉你这个
身份在目标 namespace 里能对哪些资源做什么。前者让 Agent 知道兼容性边界
（`PodDisruptionBudget v1` 需 1.21+、`IngressClass` 需 1.18+、Gateway API
是 opt-in 等），后者让 `Forbidden` 类错误在写之前就被预测到。

---

## `cluster_info()`

ℹ️ **身份 + 版本 + 计数**（新会话第一调）。

返回 4 节：

- **## Connection** — apiserver URL、是否带 bearer token（**只**给 yes / no，不给值）。
- **## Cluster** — `GitVersion` / `GitCommit[:12]` / `Major.Minor` / `Platform`。
- **## Counts** — Nodes / Namespaces / Pods / Services / Deployments 实时计数。
- 任何 apiserver 报错单节显示 `error: <status> <reason>`，**不**让整份报告空白。

**为什么不归在 `cluster_health_snapshot`**：后者是给"集群现在怎么样？"的
7 维体检（要扫事件、PDB、孤儿 PV...），开销大；这个工具**纯只读 + 单次
apiserver 调用**，是廉价身份 / 版本查询，Agent 一上来就能调。

---

## `whoami(namespace="default")`

👤 **身份 + 有效权限**。

两步走：

1. `SelfSubjectReview` 拉 `user / UID / groups`。
2. `SelfSubjectRulesReview(namespace=...)` 拉这个身份在这个 namespace 里
   能对哪些 `apiGroup / resources / verbs` 做什么。

返回 `## Identity` + `## Effective permissions in namespace 'xxx'` 两节。

**写工具返回 `Forbidden` 时先调这个**——能直接定位是 SA 权限不够还是
namespace 选错。**集群级 ClusterRole 不在这里**（`SelfSubjectRulesReview`
只看 namespace-scoped 规则），要看那些用
`get_role_bindings` / `list_resources(kind="ClusterRoleBinding", ...)`。

> ⚠️ 旧版本曾误用 `V1SubjectRulesReviewSpec`（缺 `Self` 前缀），结果是
> 这个工具从文件落地起 RulesReview 段就一直在 fallback exception 分支，
> 表面现象是 `## Effective permissions` 显示 `SelfSubjectRulesReview
> unsupported`。在 v0.1.1 已修。

---

## `find_images(image_substring, namespace=None, kinds=None)`

🔍 **反向查镜像**："哪些工作负载还在用 `nginx:1.21`？"或"哪些引用了
`registry.internal/library/`？"。

走法：扫 Deployment / StatefulSet / DaemonSet（`kinds=` 可缩窄）的
`spec.template.spec.containers` 和 `initContainers`，对 image 字符串做
**case-insensitive 子串匹配**。

- 匹配中：返回 `KIND / NAMESPACE / NAME / CONTAINER / IMAGE` 表。
- init container 行加 `[init]` 前缀（`[init] migrate`），避免与同名主容器歧义。
- 无匹配：返回 `(no workloads reference an image matching 'xxx')`——友好提示，不是空表。
- `kinds=["StatefulSet"]` 时只查 StatefulSet；`kinds=[]` 等价于 `None`（即全查）。

替换的旧做法：`list_resources(kind=Deployment) + N × get_resource_yaml` + 客户端
sub-string 匹配——Agent 端要拆 manifest、出错率高。这个工具把活儿干完一次返回。

---

## `get_events_for_object(kind, name, namespace=None, limit=50)`

📜 **对象范围事件**。

- 用 `field_selector="involvedObject.kind=<K>,involvedObject.name=<N>"` 走
  apiserver 端过滤——比 Agent 拉全 namespace 事件再 `grep` 快。
- cluster-scoped kind（`Node` / `PersistentVolume`）传 `namespace=None`，
  工具会走 `list_event_for_all_namespaces`。
- 按 `lastTimestamp` 降序排，最近的事件先露。
- 空结果：`(no events for Pod/web-1 in namespace app)`，避免 Agent 把"没数据"误读成"工具挂了"。

---

## `get_pod_logs(...)`

专为长跑 Pod 设计（数天 / 数周的日志）：

- 默认：`tail_lines=100`，`max_bytes=1 MiB`。
- `pattern=<regex>` + `context_lines=N` 按正则抓 N 行上下文。
- `label_selector=...` 一次拉多个 Pod 的日志（多 Pod 模式每行前缀 `[pod-name]`）。
- `output_format=json` 返回 `[{pod, container, time, line}]` 列表。
- 硬上限 16 MiB；超过从头部截断，附 `[truncated]` 标记。
- 当容器没有日志输出（写到文件 / 刚启动 / `tail_lines` 太小）时，工具
  返回**明确的中文提示**，避免 Agent 误以为"没调用"。
- `since_time` / `until_time` 支持 RFC3339 绝对时间窗口（"两点到四点"），
  K8s API 仅支持下界，`until_time` 客户端过滤；`strict_time=True` 丢弃
  没有 RFC3339 时间戳的行。

---

## CRD 读取：`api_version` 何时必须传

内置 `list_resources` / `get_resource` / `get_resource_yaml` /
`describe_resource` 都接受 `api_version` 参数。不传时按硬编码字典快路径
（Pod / Deployment 等）解析；找不到就 fallback 到 DynamicClient 扫所有
API group 找唯一匹配。

**什么时候必须显式传**：

- kind 名在两个 group 里都有（极少见，但 `Deployment` 被重写过的话会撞）→ 错误信息会列出所有候选
- Agent 想百分百确定是某个 CRD（避免任何一个 layer 误解析）→ 在
  `get_api_resources()` 输出里复制 `apiVersion` 字段直接传

**什么时候不用传**：内置 kind + 标准 CRD 唯一匹配。Agent 拿到报错
`Ambiguous kind 'X' — found in: [...]` 后再加。

---

## `get_certificate_expiry()`

一次性给 Agent 当前 MCP server 看见的全部证书的过期情况。

解析 4 个源（哪个有就填哪个，都空就告诉你"啥都没看见"+ 排查提示）：

1. `K8S_MCP_API_CA_CERT`（模式 A 显式 CA）
2. in-cluster SA bundle（`/var/run/secrets/kubernetes.io/serviceaccount/ca.crt`，模式 C）
3. kubeconfig 当前的 `clusters[].cluster.certificate-authority-data`（模式 B）
4. kubeconfig 当前的 `users[].user.client-certificate-data`（仅当 kubeconfig 用证书认证）

输出表 `SOURCE / SUBJECT / ISSUER / NOT_BEFORE / NOT_AFTER / DAYS_LEFT / STATUS`，
按 `DAYS_LEFT` 升序排——最快的过期先露出来。状态：`✅ valid` /
`⚠️ expires in N d (<30d)` / `❌ <7d` / `❌ EXPIRED`。自动追加
`Action needed:` 段落高亮非 `✅ valid` 的行。

**K8s apiserver 自己的 serving cert 查不到**——apiserver 不会通过 API 暴露
自己证书的 notAfter；想查那个需要 SSH 进 master 节点看
`/etc/kubernetes/pki/apiserver.crt`。这个工具的功能是让 Agent 在对话中
**主动**发现过期苗头（"你的 kubeconfig 客户端证书还有 14 天过期"），
不是替代 OS-level 巡检。

---

## 删除二次确认

### `delete_resource` —— 通用两步流程

任意 Kind（包括 Secret、Deployment、Namespace 等）走这个：

1. 调 `delete_resource(kind=..., name=..., namespace=..., confirm=False)`。
2. 工具返回 `{preview_yaml, confirmation_token, expires_in_seconds}`。
3. 把 YAML 给用户看，明确确认。
4. 再调一次，带 `confirm=True` 和 `confirmation_token`。token 里的
   `kind / name / namespace / grace_period` 必须匹配。

Token 是 HMAC-SHA256 签名（`K8S_MCP_DELETE_TOKEN_SECRET`），默认 5 分钟过期。
**生产环境务必把 `K8S_MCP_DELETE_TOKEN_SECRET` 用 `openssl rand -hex 32` 改掉**。

### 一步删除工具（恢复友好，无级联）

下列资源删了**不会**级联影响工作负载，且能重建，不强制二次确认：

| 工具 | 为什么安全 |
| --- | --- |
| `delete_pod(name, namespace, grace_period_seconds=30)` | 删了 Deployment / StatefulSet 会拉新的（恢复 / 重启原语） |
| `delete_pvc(name, namespace)` | PVC 是声明性资源，删了工作负载只 Pending 等待重新绑定 |
| `delete_configmap(name, namespace="default")` | CM 是松耦合配置数据，删了让 Pod CrashLoopBackOff，但 CM 可重建 |
| `delete_service(name, namespace="default")` | Service 是流量路由规则不是工作负载，删了 Pod 继续跑、只是 inbound 断流 |
| `delete_ingress(name, namespace="default")` | 同上，外部 HTTP(S) 路由断、Pod / Service 不动 |

对于 Secret、任何带 owner reference 的 workload、namespace、CRD 等，**必须**
走 `delete_resource` 两步流程。

---

## 批量操作三步流程（dry-run → token → confirm）

`bulk_set_image` / `bulk_restart` / `bulk_scale` / `bulk_delete_pvc` 走
**完全一样**的安全流程，因为它们能一次影响几十个工作负载：

1. `dry_run=True`（默认）—— 列出所有匹配 `label_selector` 的资源，**当前值 → 目标值**的对比表。**不写**，不发 token。
2. `dry_run=False, confirm=False` —— 同样预览，但额外返回一个 `confirmation_token`（HMAC-SHA256，5 分钟有效）。
3. `dry_run=False, confirm=True, confirmation_token=...` —— 校验 token，**只对预览时匹配的 N 个资源执行**；预览到确认之间新出现的同名 label 资源**不会被误伤**（token 的 `matched_names` 列表是权威范围）。

Token payload 把每个危险参数（`op / image / container / replicas / label_selector / kind / namespace / matched_names`）都签名进去，**改任何一项都校验失败**。`bulk_set_image` 的 token 不能拿去 `bulk_scale`，反之亦然。

**工作负载类型覆盖**：

- `bulk_set_image` / `bulk_restart` / `bulk_delete_pvc` 支持 Deployment / StatefulSet / DaemonSet
- `bulk_scale` 只支持 Deployment / StatefulSet（DaemonSet 没 `replicas` 字段，会给 `ValueError` 指向 `bulk_restart`）

**`bulk_delete_pvc`** 专门清孤儿 PVC。StatefulSet / Stateful workload 删了之后
留下的 `app=db` 标签 PVC 是最常见的清理目标。资源已不在（`404`）会被
记为 `SKIPPED (already gone)`，不算错误。

---

## `drain_node` —— 镜像 `kubectl drain`

- 先 cordon，再用 Eviction API 驱逐 Pod（尊重 PDB）。
- DaemonSet 和 emptyDir Pod 默认跳过（与 kubectl 一致）；重跑加
  `ignore_daemonsets=True` / `delete_emptydir_data=True`。
- `force=True` 绕过 PDB（raw delete）。

---

## Prometheus 工具（`prometheus_query` / `prometheus_query_range` / `pod_metrics`）

跟 `top_pods` 是**两套独立体系**：

- `top_pods` 走 Kubernetes 聚合层 API `/apis/metrics.k8s.io/...`，**只能
  从 metrics-server 拉数据**。
- Prometheus 工具走 Prometheus 自己的 HTTP API（默认 `:9090`），能查
  Prometheus 已抓取的所有指标（cAdvisor、node-exporter、各应用的
  exporter / ServiceMonitor 都行）。
- 大多数 Prometheus 部署自带 cAdvisor 指标，所以
  `pod_metrics("nginx-7c5b", "default", "cpu")` 这类查询即使没装
  metrics-server 也能用。

### 端点发现 + 桥接协议

不同集群部署方式（operator / helm / bare manifest）和 namespace 都不一样。
k8s-mcp 走"三层发现 + 两套桥接"：

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
     ClusterIP Service 不动**。**故意不传 nodePort**，由 K8s apiserver
     自己从 30000-32767 里 atomic 分配——避免客户端 scan-then-create 的
     TOCTOU race（之前有过：客户端扫完所有已用端口，随机挑一个空闲端口，
     但提交之间被别的客户端先占了，导致 422）。Agent 通过
     `list_resources(kind=Node)` 拿到节点 IP，然后用
     `http://<node-ip>:<node_port>` 去查。**不依赖 kubectl**。
   - **`TYPE=ClusterIP` 但节点 IP 不可路由**（远程 / 多层 NAT / 严格
     防火墙，常见于公有云托管 K8s）→ 没有 ClusterIP → NodePort 之外的
     兜底。用户在这种场景下需自己解决节点 IP 的可达性，例如直接
     SSH-tunnel 或改用 in-cluster 的 MCP server 模式。

3. **Agent 拿 URL 调 Prometheus 工具** —— `prometheus_query(promql,
   prometheus_url=<URL>)` / `prometheus_query_range(...)` /
   `pod_metrics(..., prometheus_url=<URL>)`。

如果 `K8S_MCP_PROMETHEUS_URL` 已经设了，工具会直接用它，跳过发现。
否则有一个"小候选名单"兜底（`monitoring/kube-prometheus-stack-prometheus`
等常用组合），兜底失败就返回中文友好的"问用户"提示。

### 桥接方案（ClusterIP → 集群外可达）

`expose_prometheus_as_nodeport` 是 ClusterIP Prometheus 的唯一推荐桥接：

| 方案 | 推荐度 | 外部依赖 | 是否需要持续进程 | 生命周期 |
| --- | --- | --- | --- | --- |
| `expose_prometheus_as_nodeport` | ⭐ ClusterIP 默认 | 无 | 否（K8s 原语） | 创建后一直在集群里，delete_resource(Service) 清理 |

**`expose_prometheus_as_nodeport` 在 read-only 模式下不可用**——它创建
新 Service，read-only 整个写流程都被拒。仍然要尊重
`K8S_MCP_NAMESPACE_ALLOWLIST`：Service 创建校验目标 namespace。
