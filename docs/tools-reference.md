# 工具参考（70 个，按功能分类）

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

### 排障 / 反向查

- `find_images(image_substring, namespace=None, kinds=None)` — 扫所有工作负载找匹配 image
- `list_pods(namespace=None, label_selector=None, field_selector=None, include_all=False)` — Pod 列表（PHASE / RESTARTS / NODE）
- `list_events(namespace=None, namespaces=None, field_selector=None, warning_only=False, limit=50)` — 集群 / namespace 事件；`namespaces=["a","b"]` 多 ns 并行 fan-out 后按 lastTimestamp 降序合并，避免误把单 ns 查询扩到全集群
- `get_events_for_object(kind, name, namespace=None, limit=50)` — 单个对象的事件
- `get_pod_logs(pod_name|label_selector, namespace, container=None, tail_lines=None, since_seconds=None, since_time=None, until_time=None, strict_time=False, previous=False, timestamps=False, pattern=None, context_lines=0, max_bytes=1MiB, output_format="text|json")` — 详见 [tools.md → `get_pod_logs`](./tools.md#get_pod_logs)
- `get_configmap(name, namespace)` — ConfigMap 内容
- `list_secrets(namespace=None, label_selector=None)` — 仅 metadata（默认）
- `get_secret_value(name, namespace, key, reveal=False)` — 单 key 窄爆炸半径读取

### 指标 / 监控

- `top_pods(namespace=None, label_selector=None, sort_by="memory")` — metrics-server
- `top_nodes(sort_by="memory")` — metrics-server
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

- `cordon_node(name)` / `uncordon_node(name)` — 节点调度开关
- `drain_node(name, ignore_daemonsets=False, delete_emptydir_data=False, force=False, grace_period_seconds=-1, timeout_seconds=60)`

### 批量（dry-run → token → confirm）

> **统一三步安全流程**，详见 [tools.md → 批量三步](./tools.md#批量操作三步流程dry-run-token-confirm)。

- `bulk_set_image(label_selector, container, image, kinds=None, namespace=None, dry_run=True, confirm=False, confirmation_token=None)`
- `bulk_restart(label_selector, kinds=None, namespace=None, dry_run=True, confirm=False, confirmation_token=None)`
- `bulk_scale(label_selector, replicas, kinds=None, namespace=None, dry_run=True, confirm=False, confirmation_token=None)`

---

## 删除

> **统一二次确认模型**，详见 [tools.md → 删除二次确认](./tools.md#删除二次确认)。

### 通用两步（Secret / 级联删除的 Kind）

- `delete_resource(kind, name, namespace=None, confirm=False, confirmation_token=None, grace_period_seconds=30)` — 任意 Kind

### 一步删除（恢复友好，无级联）

- `delete_pod(name, namespace, grace_period_seconds=30)` — 删了 Deployment / StatefulSet 会拉新的（恢复 / 重启原语）
- `delete_pvc(name, namespace)` — PVC 是声明性资源，删了工作负载只 Pending 等待重新绑定
- `delete_configmap(name, namespace="default")` — CM 可重建
- `delete_service(name, namespace="default")` — 流量规则不是工作负载
- `delete_ingress(name, namespace="default")` — 外部 HTTP(S) 路由断

### 批量

- `bulk_delete_pvc(label_selector, namespace=None, dry_run=True, confirm=False, confirmation_token=None)` — 专清孤儿 PVC
