# 工具参考（91 个，按功能分类）

> 这是**完整目录**——每个工具一行签名，按"读 / 写 / 删"分组。
> 详细使用说明（陷阱、流程、为什么这么设计）见 [tools.md](./tools.md)。

---

## 新会话开局协议

**前两件事**一定是：

1. `cluster_info()` — apiserver / 版本 / 节点 / Pod 计数
2. `whoami(namespace="<目标 ns>")` — 这个身份在目标 namespace 里能做什么

详见 [tools.md → 新会话开局协议](./tools.md#新会话开局协议)。

---

## 读（始终安全）

### 通用查询（CRD 感知）

- `list_resources(kind, namespace=None, label_selector=None, field_selector=None, limit=None, api_version=None, wide=False)` — 任意 Kind；CRD 需显式传 `api_version`；`field_selector` / `limit` 推到 apiserver 端过滤；命中 `limit` 时 footer 提示可加 selector 或提高 limit
- `search_resources(name_substring, namespace=None, kinds=None, label_selector=None, limit_per_kind=50, api_versions=None)` — 跨 kind 按名字子串搜；默认扫 ~25 个内置 kind（Pod / Deployment / Service / ...），CRD 需 `kinds=[...]` + `api_versions={kind: av}`；≥5 kind 时并行 fan-out；输出 `KIND / NAME / NAMESPACE / STATUS / AGE`，按 KIND 排序；RBAC / CRD 缺失的 kind 在 footer 里 skip 计数
- `get_resource(kind, name, namespace=None, api_version=None)` — 完整 JSON（CRD 感知）
- `get_resource_yaml(kind, name, namespace=None, reveal_secrets=False, api_version=None, include_managed_fields=False)` — YAML 清单；Secret 默认脱敏
- `describe_resource(kind, name, namespace=None, api_version=None)` — kubectl-describe 风格摘要
- `get_resource_jsonpath(kind, path, name=None, namespace=None, label_selector=None)` — 提取单个字段
- `diff_resource(yaml_content)` — 预览 apply_yaml 会改什么
- `get_api_resources(prefix=None)` — 列出集群所有 kind（含 CRD）
- `explain_resource(kind, field_path=None, api_version=None)` — `kubectl explain` via OpenAPI

### 身份 / 版本 / 计数

- `cluster_info()` — apiserver / GitVersion / 实时资源计数
- `whoami(namespace="default")` — 当前身份 + 有效 namespace-scoped 权限
- `analyze_rbac(subject=None, verb=None, resource=None, api_group=None, namespace=None)` — 只读 RBAC 分析器，四种模式：`subject=` 正查（某身份被授予的所有 rule）/ `verb+resource` 反查（谁能做这个动作 + 经由哪条 binding→role）/ `namespace=` 列该 ns 的 Role + RoleBinding / 全空 = 全集群 Role/ClusterRole/Binding 计数汇总；每种模式都把带 `*` 通配的 rule 标成 cluster-admin 风险面；反查会标出「匹配但无 binding 引用」的 unreachable role；跟 `create_role` 的 `allow_wildcard` 守门形成闭环

### 排障 / 反向查

- `diagnose_pod(name, namespace="default")` — ⭐ 单 Pod 一键深度体检；按 phase 自动分派：**Pending** 出调度诊断（突出调度器自己的 `Unschedulable` 裁决 + PVC 绑定 + requests 汇总，不重算每节点拟合）/ **Running / CrashLoop / Error** 出运行时诊断（每容器 state/lastState、OOMKilled、exit code、restart，CrashLoop 时自动 tail previous 容器最后 20 行日志）；尾部附最近 events。与 `cluster_health_snapshot` 互补（那个给广度，这个给深度）
- `diagnose_deployment(name, namespace="default")` — ⭐ 单 Deployment 一键深度体检；输出 4 段：**Rollout**（`desired/ready/updated/available` 汇总 + `Progressing` condition 自己的 verdict — `NewReplicaSetAvailable` ✅ 或 `ProgressDeadlineExceeded` ❌）/ **ReplicaSets**（owned RS 表，pod-template-hash、desired/current/ready、首容器 image — old vs new image 差异一眼可见）/ **New ReplicaSet**（ready 数 + pod 阶段表；如发现 `Pending` 或 `CrashLoopBackOff` pod，结尾给字面 `Next step: call diagnose_pod(name=<pod>, namespace=<ns>)`，agent 不用猜下一步）/ **Recent events**。与 `diagnose_pod`（单 Pod 深度）和 `cluster_health_snapshot`（集群广度）互补 — 这是中间层：单 Deployment 深度
- `gpu_cluster_overview()` — NVIDIA GPU 节点、`nvidia.com/*` capacity / allocatable、活跃 GPU Pod 需求与可选 ClusterPolicy 摘要
- `gpu_node_inspect(name)` — 单 GPU 节点的扩展资源、NVIDIA 标签、污点与已调度 GPU Pod
- `gpu_workload_inspect(name, namespace="default", kind="Pod")` — Pod / Deployment / Job 的 GPU limits、节点放置和调度器裁决
- `gpu_pending_workloads(namespace=None, limit=50)` — 带 `nvidia.com/*` limits 的 Pending Pod 及其 `Unschedulable` 原因
- `gpu_diagnose(operator_namespace="gpu-operator")` — GPU 节点、ClusterPolicy、GPU Operator Pod 与 Pending GPU workload 的一键只读诊断
- `gpu_metrics_catalog(metric_prefix="DCGM_", limit=100, prometheus_url=None)` — 从 Prometheus 发现真实存在的 DCGM / GPU 指标及其 series 数
- `gpu_utilization_overview(utilization_metric="DCGM_FI_DEV_GPU_UTIL", memory_used_metric="DCGM_FI_DEV_FB_USED", memory_total_metric="DCGM_FI_DEV_FB_TOTAL", prometheus_url=None)` — 每 GPU 最新利用率与显存原始指标概览
- `gpu_workload_utilization(pod_name, namespace="default", metric_name="DCGM_FI_DEV_GPU_UTIL", prometheus_url=None)` — 按 `namespace` / `pod` 标签读取一个 Pod 的 GPU 指标样本
- `gpu_utilization_history(duration="1h", step="5m", metric_name="DCGM_FI_DEV_GPU_UTIL", namespace=None, pod_name=None, limit=20, prometheus_url=None)` — 有界时间窗口的每 GPU / Pod 利用率统计，输出 min / avg / max / latest 而非倾倒全部数据点
- `analyze_networkpolicy(namespace, pod=None)` — 🔍 NetworkPolicy 只读连通性 / 覆盖分析器，闭合 `create_networkpolicy` 的验证环。`pod=` 视图评估 `matchLabels` + `matchExpressions`，列出每个 selecting policy 的 ingress/egress 规则（peers + ports），输出每方向实际姿态（selecting policy 列出该 `policyType` → `🔒 default-deny`，否则 `🔓 default-allow`）；`namespace=` 视图是 coverage 扫描：每个 pod 的 in/out 姿态 + 暴露面（无 policy 选中的 pod）+ policy 清单（deny-all 标记）。声明的策略图，是否真正生效要看 CNI 插件
- `explain_pod(namespace, name)` — 🧭 Pod top-down 看相：沿 `ownerReferences` 爬到顶层 controller（Deployment / StatefulSet / DaemonSet / Job），同顶层 controller 的 sibling pods 列表（用 pod-template labels 查），加上 Pod spec 关键字段（node / serviceAccount / 容器 image）。owner 链中途炸（CRD 缺失/对象已删）会显示断点不抛异常。与 `diagnose_pod` 互补（那个关注运行时；这个关注静态归属 + 调度布局）
- `analyze_resource_usage(namespace="default", kind="Pod", mode="missing_requests")` — 📊 静态 requests/limits 审计：扫 namespace 找 requests/limits 问题。`mode=` 三选 — `missing_requests`（容器无 requests，Burstable QoS 可被驱逐）/ `missing_limits`（容器无 limits，CPU 无上限）/ `inconsistent`（limits < requests，scheduler 静默改回 requests，manifest 大概率错的）；`kind=` 为 Deployment/StatefulSet/DaemonSet 时扫 pod template 容器；`kind=Pod` 跳过 workload-owned pod（避免重复）只看孤儿 pod。配合 `diagnose_pod`（运行时）和 `cluster_health_snapshot`（广度）使用，这个给静态卫生分
- `find_images(image_substring, namespace=None, kinds=None)` — 扫所有工作负载找匹配 image
- `list_pods(namespace=None, label_selector=None, field_selector=None, include_all=False)` — Pod 列表（PHASE / RESTARTS / NODE）
- `exec_pod(pod_name, command, namespace="default", container=None, timeout_seconds=30)` — ⚠️ 高权限：批模式在 Pod 容器里跑命令（argv list，不走 shell；要 pipe/redirect 显式 `["sh","-c","..."]`）；自动选单容器 Pod 的第一个容器，多容器必须显式 `container=`；stdout / stderr 分离 + 真 exit code；超时断开 WebSocket（pod 里命令可能不终止）
- `list_events(namespace=None, namespaces=None, field_selector=None, warning_only=False, limit=50)` — 集群 / namespace 事件；`namespaces=["a","b"]` 多 ns 并行 fan-out 后按 lastTimestamp 降序合并，避免误把单 ns 查询扩到全集群
- `get_events_for_object(kind, name, namespace=None, limit=50)` — 单个对象的事件
- `get_pod_logs(pod_name|label_selector, namespace, container=None, tail_lines=None, since_seconds=None, since_time=None, until_time=None, strict_time=False, previous=False, timestamps=False, pattern=None, context_lines=0, max_bytes=1MiB, output_format="text|json")` — 详见 [tools.md → `get_pod_logs`](./tools.md#get_pod_logs)
- `get_configmap(name, namespace)` — ConfigMap 内容
- `list_secrets(namespace=None, label_selector=None)` — 仅 metadata（默认）
- `get_secret_value(name, namespace, key, reveal=False)` — 单 key 窄爆炸半径读取

### 指标 / 监控

- `top_pods(namespace=None, label_selector=None, sort_by="memory", prometheus_url=None)` — ⭐ 三档级联：`metrics-server` → Prometheus（cAdvisor / node-exporter）→ `bootstrap_metrics_server`（仅写权限允许时）。详见 [tools.md → top_pods / top_nodes 级联](./tools.md#top_pods--top_nodes-级联metrics-server--prometheus--bootstrap)
- `top_nodes(sort_by="memory", prometheus_url=None)` — 同上（Node 维度，node-exporter）
- `bootstrap_metrics_server(manifest_url=None, kubelet_insecure_tls=True, wait_seconds=30)` — 🛠 [写] 应用 upstream `components.yaml` 到 `kube-system`、patch `--kubelet-insecure-tls`、等 ready。幂等；`top_*` 级联失败时也会自动触发（一次性）
- `prometheus_query(promql, time=None, prometheus_url=None)` — Prometheus 即时 PromQL
- `prometheus_query_range(promql, start, end, step="30s", prometheus_url=None)` — 范围查询
- `pod_metrics(pod_name, namespace, metric="cpu", range="5m", prometheus_url=None)` — cAdvisor 指标（cpu / memory / network_rx / network_tx / fs_reads / fs_writes）
- `find_prometheus_service(namespace=None)` — 扫 Prometheus Service + 字面推荐签名
- `expose_prometheus_as_nodeport(namespace, service_name, name_suffix="-np")` — ⭐ ClusterIP Prometheus 桥接（创建 NodePort 副本 Service）

### 集群状态

- `cluster_health_snapshot(namespaces=None, events_minutes=60, restart_threshold=3)` — ⭐ 11 维度集群体检（Nodes / Resource Usage / Pending / Abnormal Restarts / Pod Distribution / Image Pull / Workloads / HPA / Orphan PVs / Certs / Events）
- `get_certificate_expiry()` — kubeconfig / SA bundle / apiserver CA 全部证书过期
- `rollout_status(kind, name, namespace, timeout_seconds=60, watch=False)` — Deployment / StatefulSet / DaemonSet 状态
- `rollout_history(kind, name, namespace)` — 镜像历史（传给 `rollout_undo`）
- `wait_resource(kind, name, namespace=None, for_condition=None, for_jsonpath=None, jsonpath_value=None, timeout_seconds=60)` — 等条件满足

### 主动推送

- `notify(message, level="info", notifier_name=None, title=None)` — 把只读结果推到 webhook；按 notifier 配置的 `type` 走不同 payload：
  - `feishu` 纯文本 / `feishu_post` 飞书富文本 / **`feishu_card`** 飞书交互卡片（header 颜色随 level 变化，每个 `## 章节` 渲染成独立 lark_md 块，生产推荐） / `slack` / `wecom` / `generic`
  - **URL scheme gate**：默认仅 `https://`；`http://` 需 `K8S_MCP_NOTIFIER_URL_ALLOW_HTTP=true`（local-dev 用）；`file://` / `gopher://` 等一律拒收

---

## 写（受 read-only + namespace-allowlist 限制）

### Apply / Replace

- `apply_yaml(yaml_content)` — 单文档或多文档清单
- `replace_resource(yaml_content)` — PUT 带 ResourceVersion
- `add_label(kind, name, key, value, namespace=None, api_version=None)` — JSON Patch `add`，原子改单个 label（不丢其他字段；RFC 6901 转义支持 `app.kubernetes.io/name` 这类带 `/` 的 key）
- `remove_label(kind, name, key, namespace=None, api_version=None)` — strategic-merge `null` 值原子移除单个 label（idempotent，缺失 = no-op；等价 `kubectl label foo bar-`）

### 工作负载创建

- `create_deployment(name, image, namespace=None, replicas=None, container_name=None, ports=None, env=None, labels=None, resources=None, image_pull_policy=None)`
- `create_statefulset(name, image, service_name, namespace=None, replicas=None, ...)`
- `create_job(name, image, namespace=None, command=None, args=None, env=None, resources=None, restart_policy="Never", backoff_limit=None)` — 一次性任务
- `create_cronjob(name, image, schedule, namespace=None, command=None, args=None, env=None, resources=None, restart_policy="OnFailure")` — 定时任务

### 工作负载运维

- `scale_workload(kind, name, namespace, replicas)` — 改副本数
- `restart_workload(kind, name, namespace)` — 滚动重启
- `set_image(kind, name, namespace, container, image)` — 改镜像
- `set_resources(kind, name, namespace, container, requests={}, limits={})` — 改资源
- `rollout_undo(kind, name, namespace=None, to_revision=None)` — 回滚
- `expose_workload(...)` — Workload → Service 暴露

### Service / Ingress

- `create_service(name, namespace, ports, selector, type=None, ...)`
- `create_ingress(name, namespace, rules, ...)`
- `expose_workload(workload_kind, workload_name, namespace, port, ...)` — Workload → Service 暴露
- `get_endpoints(service_name, namespace="default")` — 🔍 Service → Pod 映射诊断（EndpointSlice 优先；Endpoints 兜底）
- `expose_prometheus_as_nodeport(namespace, service_name, name_suffix="-np")` —（见上"指标"）

### 存储

- `create_pvc(name, namespace, size, access_modes=None, storage_class=None, volume_name=None, labels=None)` — `volume_name` 显式绑 hostPath PV
- `validate_pv_hostpath_paths()` — 列出 hostPath PV + 一键 `ssh` 检查
- `bootstrap_local_path_provisioner(set_as_default=True, apply_immediately=True)` — 一键装 Rancher local-path 给无 SC 的 dev/test 集群

### RBAC / NetworkPolicy / ServiceAccount

- `create_role(name, namespace, rules, allow_wildcard=False)` — 默认拒绝 `verbs=["*"] ∧ resources=["*"] ∧ apiGroups=["*"]` 三重通配（= cluster-admin），必须显式 `allow_wildcard=True` 才能建；防漏 `resources` / `apiGroups` 时静默授予 cluster-admin
- `create_rolebinding(name, namespace, role, subjects)`
- `create_clusterrole(name, rules, allow_wildcard=False)` — 同上
- `create_clusterrolebinding(name, role, subjects)`
- `create_serviceaccount(name, namespace, image_pull_secrets=[])` — `image_pull_secrets` 可选
- `create_networkpolicy(name, namespace, pod_selector, policy_types=["Ingress"|"Egress"], ingress=[], egress=[])`
- `create_hpa(name, namespace, target, min_replicas, max_replicas, metrics)`
- `create_pdb(name, namespace, selector, min_available=None, max_unavailable=None)`

### 节点运维

- `list_nodes(label_selector=None, include_unschedulable=True)` — 节点专属列视图（ROLE / STATUS / INTERNAL_IP / TAINT_SUMMARY / AGE）
- `label_node(name, key, value=None)` / `unlabel_node(name, key)` — JSON Patch 原子标签增删（RFC 6901 转义）
- `taint_node(name, taint)` / `untaint_node(name, taint=None)` — 单条污点增删；`untaint_node(taint=None)` 清空所有污点
- `cordon_node(name)` / `uncordon_node(name)` — 节点调度开关
- `drain_node(name, ignore_daemonsets=False, delete_emptydir_data=False, force=False, grace_period_seconds=-1, timeout_seconds=60)`

### 命名空间 / 配置 / 密钥

- `create_namespace(name, labels=None, annotations=None)` — cluster-scoped；`K8S_MCP_NAMESPACE_ALLOWLIST` 设了时拒
- `create_configmap(name, namespace, data=None, yaml_content=None, labels=None)` — `data` 或 `yaml_content` 二选一
- `create_secret(name, namespace, data=None, string_data=None, secret_type="Opaque", labels=None)` — `string_data` 自动 base64；空值拒绝

---

## 删除

> v0.5.2 起删除是**单步**：`delete_resource(kind, name, namespace=None, grace_period_seconds=30)`。原先的"预览 → confirm_token → 真删"两步流程已移除——LLM agent 既发请求又提交 token，HMAC 二次确认不构成有效防护。守门由 `K8S_MCP_READ_ONLY`（全局 kill switch）+ `K8S_MCP_NAMESPACE_ALLOWLIST`（namespace 白名单）承担。

### 通用单步（任意 Kind）

- `delete_resource(kind, name, namespace=None, grace_period_seconds=30)` — 任意 Kind；cluster-scoped 资源（无 namespace）不设 `namespace`。受 `READ_ONLY` 和 `NAMESPACE_ALLOWLIST` 守门。
