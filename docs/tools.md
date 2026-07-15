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

## 删除（v0.5.2 起单步）

### `delete_resource` —— 单步删除

任意 Kind（包括 Secret、Deployment、Namespace 等）走这个：

```python
delete_resource(kind, name, namespace=None, grace_period_seconds=30)
```

- **守门**：`K8S_MCP_READ_ONLY=true` 时直接拒；设了
  `K8S_MCP_NAMESPACE_ALLOWLIST` 时目标 namespace 不在白名单（或
  cluster-scoped 资源无 namespace）也拒。
- **找不到资源**：apiserver 返回 404 → `LookupError("... already gone ...")`。
- **未知 Kind**：`ValueError("Unknown kind: ...")`,建议先
  `get_api_resources()` 看支持列表。

### 一步删除工具（已删除）

v0.5.0 起：`delete_pod` / `delete_pvc` / `delete_configmap` / `delete_service`
/ `delete_ingress` 全部移除，统一走 `delete_resource`。批量工具
（`bulk_set_image` / `bulk_restart` / `bulk_scale` / `bulk_delete_pvc`）
也一并移除 —— `scale_workload` / `restart_workload` / `set_image` 已支持
列表型 `name=` 参数，PVC 批量清理改用 `apply_yaml` + `label_selector` 收敛后
走 `delete_resource`。

### 二步预览流程（v0.5.2 起移除）

v0.4.x ~ v0.5.1 的 "预览 + HMAC 确认 token" 流程已移除。理由：在
LLM-driven 场景里同一个 agent 既发 preview 调用又发 confirm 调用，
HMAC token 校验不构成任何额外防护（agent 自己就能伪造），徒增配置项。
守门完全交给 `READ_ONLY` + `NAMESPACE_ALLOWLIST`。

---

## `drain_node` —— 镜像 `kubectl drain`

- 先 cordon，再用 Eviction API 驱逐 Pod（尊重 PDB）。
- DaemonSet 和 emptyDir Pod 默认跳过（与 kubectl 一致）；重跑加
  `ignore_daemonsets=True` / `delete_emptydir_data=True`。
- `force=True` 绕过 PDB（raw delete）。

---

## Prometheus 工具（`prometheus_query` / `prometheus_query_range` / `pod_metrics`）

跟 `top_pods` 是**两套独立体系**：

- `top_pods` 优先走 Kubernetes 聚合层 API `/apis/metrics.k8s.io/...`
  （metrics-server），失败时**自动 fallback 到 Prometheus**（cAdvisor +
  node-exporter）—— 见下面 [top_pods / top_nodes 级联](#top_pods--top_nodes-级联metrics-server--prometheus--bootstrap)。
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

---

## `top_pods` / `top_nodes` —— 级联（metrics-server → Prometheus → bootstrap）

签名：

```python
top_pods(
    namespace: str | None = None,
    label_selector: str | None = None,
    sort_by: str = "memory",            # "cpu" or "memory"
    prometheus_url: str | None = None,
) -> str

top_nodes(
    sort_by: str = "memory",
    prometheus_url: str | None = None,
) -> str
```

`top_pods` / `top_nodes` 是 `kubectl top` 的等价工具，但在 metrics-server
缺失时**不会傻乎乎地 404**——它走三档级联，让 `top` 几乎在所有集群上
都能直接用：

1. **metrics-server（最快路径）** —— 走 K8s 聚合层
   `/apis/metrics.k8s.io/v1beta1/...`。绝大多数装 Prometheus 的集群也
   装了这个，所以 path 1 经常一次就通。
2. **Prometheus fallback（绝大多数集群都能命中）** —— metrics-server
   404 时自动改走 Prometheus（通过上面
   [端点发现 + 桥接协议](#端点发现--桥接协议) 拿到的 URL），
   用 cAdvisor 抓的 `container_cpu_usage_seconds_total[5m]` /
   `container_memory_working_set_bytes`（Pod）和
   node-exporter 抓的 `node_cpu_seconds_total{mode!="idle"}[5m]` /
   `node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes`（Node）
   现算。`label_selector` 在 Prometheus 路径上会被翻译成 pod-name 正则
   （多一次 apiserver list，但只一次）；找不到匹配的 pod 时退回到
   namespace-only filter 并标注。
3. **`bootstrap_metrics_server`（仅当 1+2 都失败且有写权限时自动跑）** ——
   当 path 1 404 且 path 2 也连不上 Prometheus 时，**如果**
   `READ_ONLY=false` **且** `kube-system` 在 `NAMESPACE_ALLOWLIST` 里，
   自动 apply 上游 `components.yaml` 到 `kube-system`、patch 上
   `--kubelet-insecure-tls`（自建集群的 kubelet 自签证书场景）、等
   Deployment ready，然后回到 path 1 重试。**整个过程 agent 看不见**：
   agent 只看到一个 `top_pods()` 调用，最终返回一张表（或者一条
   "bootstrap 失败，请检查 kube-system 权限" 的提示）。

**错误传播**：三档全失败时 `top_pods` / `top_nodes` 抛
`RuntimeError`，**字面**列出下一步可以做什么：

```
top_pods: neither metrics-server nor Prometheus is reachable.
  - metrics-server: not installed (apiserver 404 on /apis/metrics.k8s.io)
  - prometheus: Prometheus is not auto-discoverable
  - bootstrap_metrics_server: SKIPPED (kube-system is not in
    K8S_MCP_NAMESPACE_ALLOWLIST).
    Next steps (pick one):
      a) Allow `kube-system` in K8S_MCP_NAMESPACE_ALLOWLIST and re-call
      b) Manually install metrics-server:
         kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.7.2/components.yaml
      c) Install Prometheus (kube-prometheus-stack) and let the agent
         discover it via `find_prometheus_service()`.
  - OR call prometheus_query(<PromQL>, prometheus_url=<URL>) directly
    once a Prometheus URL is known.
```

这样 agent 不会卡在 "metrics-server 没装我该怎么办" 的循环里——`c)` 和
最后那条 `prometheus_query(...)` 提示它直接绕过 `top_*` 拿数据。

### `bootstrap_metrics_server` —— 显式触发

签名：

```python
bootstrap_metrics_server(
    manifest_url: str | None = None,        # 默认 upstream release URL
    kubelet_insecure_tls: bool = True,      # 自建集群 patch `--kubelet-insecure-tls`
    wait_seconds: int = 30,
) -> str
```

Agent 想在执行 `top_pods` 之前预先把 metrics-server 装好时显式调用；
或者 `top_pods` 自动 bootstrap 失败后手动重试。**幂等**：检测到
`Deployment/metrics-server` 已存在直接返回 `status=AlreadyInstalled`
不再 apply。

离线 / 私有镜像：覆盖 `K8S_MCP_METRICS_SERVER_MANIFEST_URL` 指向自托管
manifest（env 段 [dev / 离线](./env.md#dev--离线)）。

**One-shot gate**：级联内的自动 bootstrap 是**单次**的——同一个进程内
首次失败后再调用 `top_pods` 不会再次 retry，避免 agent 在循环里反复
apply + probe 把 apiserver 打满。重启 MCP server 后重置。

### 何时不该用 `top_pods` / `top_nodes`

- 想看**网络 rx/tx、磁盘 r/w、单 container 拆分** —— `top_pods` 只
  输出 Pod 维度的 CPU / memory 聚合。要更细直接 `prometheus_query(...)`
  查 `container_network_*` / `container_fs_*` / `*_bytes_total`。
- 想看**某个 Pod 在过去一小时**的曲线 —— `top_*` 是 instant；用
  `prometheus_query_range()`。
- 想按自定义指标排序（比如 GPU 利用率）—— 同样走 `prometheus_query`
  + 客户端排序。

---

## `exec_pod` —— 容器内批模式执行

⚠️ **高权限**。与 `kubectl exec` 等价，但**不是**交互 shell。

签名：

```python
exec_pod(
    pod_name: str,
    command: list[str],
    namespace: str = "default",
    container: str | None = None,
    timeout_seconds: int = 30,
) -> str
```

### 三个常踩的坑

1. **`command` 是 argv list，不走 shell** —— 想要 `pipe` / `redirect` /
   glob 必须显式 `["sh", "-c", "..."]`。这是 K8s exec 协议的设计
   （避开 shell-injection），不是 bug。
2. **多容器 Pod 必须显式 `container=`** —— 单容器 Pod 会自动选第一个；
   多容器不传会报"ambiguous, pick one: <name1>, <name2>"。这是为了
   避免 Agent 误选 sidecar。
3. **超时是 wall-clock** —— K8s exec 协议**没有 cancel**，超时只会
   断开 WebSocket，pod 里的命令可能还在跑。要真杀得 SSH 进节点 / 用
   metrics-server 看进程 / 进容器自己 kill。

### 适用 vs 不适用

- ✅ 跑一次性探针（`ls` / `cat config` / `curl localhost:8080/health` /
  `ps aux` 看进程）
- ✅ 拉取一次性 dump（`tcpdump -c 100 -w -` / `jstack` / `jmap -histo`）
- ❌ 长时间跑的后台任务（用 `kubectl cp` + nohup / 或者开个 debug
  container，而不是 `exec_pod`）
- ❌ 交互式 shell（stdin 没用，`bash -i` 会立刻退）

### 安全守门

- 跟其他写工具一致：`K8S_MCP_READ_ONLY=true` 直接拒收
- `K8S_MCP_NAMESPACE_ALLOWLIST` 不含目标 ns 拒收
- **不做命令白名单** —— 信任 K8s RBAC；能 pods/exec 就能跑任意命令

---

## `add_label` / `remove_label` —— 单 label 原子改

跟 `apply_yaml` / `replace_resource` 的边界：**只动一个 label 时**用这两个，
不要拉整份 manifest 改完再 PUT 回去（会丢 `managedFields` / status / 其他
labels / annotations，还要管 ResourceVersion）。

```python
add_label(kind, name, key, value, namespace=None, api_version=None) -> str
remove_label(kind, name, key, namespace=None, api_version=None) -> str
```

- **`add_label`** 走 JSON Patch `add`（RFC 6902），**原子**。`value` 字符串。
  key 含 `/` / `~` 自动 RFC 6901 转义（`app.kubernetes.io/name` 安全）。
- **`remove_label`** 走 strategic-merge patch `null` 值（`kubectl label
  foo bar-` 等价）；**idempotent** —— key 不存在 = no-op，不会因为并发
  改 label 互相覆盖。

`label_selector` 类工作（一次改 N 个资源的同一 label）走 `apply_yaml`，
不在 `add_label` 的范围。

---

## `search_resources` —— 跨 kind 名字子串搜

跟 `list_resources(kind=X)` 的边界：**不知道在哪个 kind / namespace**
时用。`list_resources` 一次只看一个 kind，要找"那个名字像 X 的东西"得
循环 N 次；`search_resources` 默认扫 ~25 个内置 kind，**一次**返回。

签名：

```python
search_resources(
    name_substring: str,
    namespace: str | None = None,
    kinds: list[str] | None = None,         # 缩窄到这几种
    label_selector: str | None = None,
    limit_per_kind: int = 50,
    api_versions: dict[str, str] | None = None,  # CRD: {kind: "group/v1"}
) -> str
```

输出表 `KIND / NAME / NAMESPACE / STATUS / AGE`，按 KIND 排序。

性能特性：≥ 5 个 kind 时走 `ThreadPoolExecutor`（max 8 workers）并发 fan-out；
RBAC 拒绝 / CRD 未装的 kind 静默 skip，footer 给 skip 计数。

CRD 场景：`kinds=["MyCRD"]` + `api_versions={"MyCRD": "example.com/v1"}`，
不加 `api_versions` DynamicClient 会扫所有 group 找唯一匹配，CRD 重名
时直接 `Ambiguous` 报错。

