# Roadmap

发版流程（PyPI OIDC + GitHub Actions + 6 个 Release）就位后，下一步是项目本身的健壮性、性能和 LLM 友好度。
每个 Phase 一个独立 PR，按下面勾选状态跟踪。

> 本文件与 `~/.claude/plans/` 下的最新 plan 文件保持一致；任何变更先改 plan 再改这里。

---

## Phase A — 快速止血

bug 性质，影响所有用了 notify 或 cluster_health_snapshot 的部署。

- [x] **A1 · notifier 健壮性** — `src/k8s_mcp/tools/notifier.py`
  - [x] retry 3 次，指数退避（0.5s / 1s / 2s），覆盖 500/502/503/504
  - [x] payload 大小 guard：Slack 40KB / WeCom 4KB / Feishu 30KB，超长时附 `⚠️ truncated from N bytes`
  - [x] `requests.Session()` 模块级连接池 + Retry
  - [x] `tests/test_notifier.py`：mock retry + 截断 + 超时

- [x] **A2 · health.py N+1 修复** — `src/k8s_mcp/tools/health.py`
  - [x] `_section_workloads` 改用 `list_*_for_all_namespaces` + 客户端按 `nss` 过滤
  - [x] `tests/test_health.py`：断言 `list_*_for_all_namespaces` 只调用 1 次
  - 目标：50-ns 集群 `cluster_health_snapshot` 从 ~30s 降到 ~5s
  - PR: v0.4.2

---

## Phase B — 工具整合（LLM 友好度）

LLM 选工具靠 description；当前 72 个工具里没有重叠（v0.5.0 已清除 9 个 deprecated tool）。

- [x] **B1 · 删除 5 个 kind-specific `delete_*`** — v0.5.0
  - [x] `delete_pod`（`pods.py`）→ `delete_resource(kind="Pod")`
  - [x] `delete_service` / `delete_ingress`（`service.py`）→ `delete_resource(kind=...)`
  - [x] `delete_configmap`（`configmap.py`）→ `delete_resource(kind=...)`
  - [x] `delete_pvc`（`storage.py`）→ `delete_resource(kind=...)`
  - [x] `delete_pod_rejects_*` 测试改用 `delete_resource(kind="Pod")`（`tests/test_secret_discovery_diff.py`）

- [x] **B2 · `bulk_*` → 单工具列表变体** — PR: #12
  - [x] `scale_workload(name: str | list[str], ...)` 已支持列表，`restart_workload` / `set_image` 同理
  - [x] `bulk_scale` / `bulk_restart` / `bulk_set_image` / `bulk_delete_pvc` 已标 @deprecated
  - [x] 修复 `bulk.py` 中重复的 label_selector 检查 bug（2 处）

- [x] **B1+B2 收尾 · 删除 4 个 deprecated `bulk_*`** — v0.5.0
  - [x] `bulk_set_image` / `bulk_restart` / `bulk_scale`（`src/k8s_mcp/tools/bulk.py` 整个文件删除）
  - [x] `bulk_delete_pvc`（`storage.py`）
  - [x] 配套测试删除：`tests/test_bulk.py` / `test_bulk_delete_pvc.py` / `test_deprecation_markers.py` / `test_delete_low_risk.py`
  - [x] 测试侧 `bulk._verify_bulk_token` / `storage.bulk_delete_pvc` 测试段同步删除（`test_caller_binding.py` / `test_write_safety.py`）

- [x] **B3 · `list_pods` vs `list_resources(kind="Pod")` 边界澄清** — docstring 已注明；`list_pods` 仅在需要 PHASE/RESTARTS/NODE 列或 Succeeded/Failed filter 时用，其余走 `list_resources`

- [x] **B4 · 6 对工具的 description 边界文案** — docstring 中已显式标注与对应工具的分工

- [x] **B5 · 工具数修正 + inventory 测试** — v0.5.0
  - [x] README.md / README.en.md：80 → 72
  - [x] `docs/tools-reference.md` / `docs/tools.md` / `docs/architecture.md` 同步
  - [x] `tests/test_tool_inventory.py`：断言工具数 == 72

---

## Phase C — 性能 / 资源

深水区，影响频繁调用 Prometheus / 多 pod 日志 / 大 CRD 集群。

- [ ] **C1 · 连接池 + 超时分级**
  - 新增 `src/k8s_mcp/_http.py`：`shared_requests_session()` (thread-local, pool_connections=10)
  - `prometheus._prom_get` 改用 session
  - `logs._fetch_logs_multi` 多 pod 改 `ThreadPoolExecutor(max_workers=8)` 并发
  - `_PROM_HTTP_TIMEOUT` 拆 `(5, 30)` connect/read

- [x] **C2 · OpenAPI cache 大小上限** — `discovery.py` — v0.5.0
  - [x] `_OPENAPI_CACHE_MAX_BYTES = 8 MiB`，超 cap 清空强制下次重读
  - [x] 顺带修了 `kubernetes.client.OpenApiApi` 在 client v36+ 已删除的潜在 bug（改走 `api.call_api("/openapi/v3", "GET")`）
  - [x] `tests/test_discovery_cache_cap.py` 8 个测试覆盖 size-cap / TTL / 重置

- [ ] **C3 · auth.py 双路径去重** — `auth.py`
  - 抽 `_default_kubeconfig_path()` helper，line 74-87 和 122-126 共用

---

## Phase D — 文档落地

- [x] **D1 · 本文件（ROADMAP.md）** — v0.5.0 同步勾选状态
- [x] **D2 · CHANGELOG [Unreleased]** — Phase A/B/C 摘要

---

## 已完成（v0.3.0 及之前）

- [x] PyPI Trusted Publishing via OIDC
- [x] GitHub Actions CI（lint + pytest 矩阵 3.11/3.12/3.13）
- [x] GitHub Actions release.yml（tag 触发 build → publish → release）
- [x] 6 个 GitHub Releases（v0.1.1 ~ v0.3.0）
- [x] `docs/publishing.md` 写完整发版文档
- [x] `release_workflow.md` / `release_pypi_trusted_publisher.md` 写入 memory

---

## v2+（不本轮范围）

- MCP HTTP/SSE 传输
- Multi-cluster routing
- Helm / Kustomize 集成
- 长连接 log streaming（`exec_pod` 短批模式已在 v0.4.3）
- RBAC 工具（list/grant role）
- Docker image / Helm chart 发布
- Slack 签名验证 / 端到端 idempotency 协议

---

## v0.6.0 — 节点运维补全 + 命名空间 / ConfigMap / Secret 快捷（next-iteration）

**主题**：补齐 ROADMAP `C 后续 / v2+ 不本轮范围` 里"高频运维"清单的最关键 9 个工具。修复文档/常量漂移（`pyproject.toml` description 错了几个版本，`Changelog` URL 指错文件）。

- [x] **`list_nodes`** — 节点专属列视图（ROLE / STATUS / INTERNAL_IP / TAINT_SUMMARY / AGE）。
- [x] **`label_node` / `unlabel_node`** — JSON Patch 原子操作，RFC 6901 转义。
- [x] **`taint_node` / `untaint_node`** — 单条污点增删；效果名 client-side 校验。
- [x] **`get_endpoints`** — 双路 Service → Pod 诊断（EndpointSlice 优先，Endpoints 兜底）。
- [x] **`create_namespace`** — cluster-scoped 命名空间快捷。
- [x] **`create_configmap`** — ConfigMap 双模式入口（data dict / 原始 YAML）。
- [x] **`create_secret`** — Secret 双模式入口（string_data 自动 base64 / 原始 base64）；空值拒绝。
- [x] **文档/常量漂移修复** — `pyproject.toml` description 80→73、Changelog URL → `CHANGELOG.md`；`tests/test_tool_inventory.py` docstring 与常量对齐。
- [x] **测试覆盖** — 3 个新文件、46 个新单元测试（`test_node_ops_extended.py` / `test_endpoints.py` / `test_namespace_configmap_secret.py`）。

工具总数 73 → 82。`pyproject.toml` version 仍为 0.5.3（等待用户确认 + 走发版流程再 bump 到 0.6.0）。

不在本轮范围（保持 v2+）：

- `port_forward` / `copy_to_pod` — 需 WebSocket tar/stream 协议，独立 v2 spec。
- ROADMAP Phase C1（HTTP session 复用 + Prometheus 重写）—— 单测性能瓶颈不显著；保留到下一轮。
- ROADMAP Phase C3（auth.py kubeconfig 路径去重）—— 重复代码量小（≈10 行），收益低；保留。
- MCP HTTP/SSE 传输、Helm/Kustomize 集成、多集群路由——独立 v2 完整 spec。

---

## Phase E — 读类分析器（v0.4.3）

v0.4.3 在已有 read 类工具的基础上新增 4 个"读"型分析器，闭合了
apply → write → 验证 这条链上的"验证"环节。**Read-only。**

- [x] **E1 · `diagnose_pod`** (`src/k8s_mcp/tools/diagnostics.py`)
  - 单 Pod 一键体检：Pending 走调度诊断（看 kube-scheduler 自己的 `Unschedulable` 裁决，不重算每节点 fit），Running/CrashLoop 走运行时诊断
  - 自动 tail crash container 的 previous logs
  - 14 tests in `tests/test_diagnose_pod.py`
  - PR: #13

- [x] **E2 · `analyze_rbac`** (`src/k8s_mcp/tools/rbac.py`)
  - 多模式 RBAC 只读 inspector：subject 正查 / verb+resource 反查 / namespace / cluster summary
  - 通配规则标 cluster-admin 风险面
  - 24 tests in `tests/test_analyze_rbac.py`
  - PR: #13

- [x] **E3 · `analyze_networkpolicy`** (`src/k8s_mcp/tools/networkpolicy.py`)
  - NetworkPolicy 连通性 + 覆盖分析器：pod 视图（评估 podSelector / matchExpressions）+ namespace 覆盖（暴露面 + deny-all 标记）
  - 14 tests in `tests/test_analyze_networkpolicy.py`
  - PR: #13

- [x] **E4 · `analyze_resource_usage`** (`src/k8s_mcp/tools/resource_usage.py`)
  - 静态 requests/limits 三种 mode 审计（missing_requests / missing_limits / inconsistent）
  - Pod mode 自动跳过 workload-owned pod
  - 10 tests in `tests/test_resource_usage.py`

- [x] **E5 · `explain_pod`** (`src/k8s_mcp/tools/explain.py`)
  - Pod top-down 看相：owner 链（沿 `ownerReferences` 爬到顶层 controller）+ siblings（pod-template labels）+ spec essentials
  - 8 tests in `tests/test_explain_pod.py`

- [x] **E6 · `search_resources`** 跨 kind name 子串搜索（fans out `ThreadPoolExecutor` ≥5 kinds）
- [x] **E7 · `add_label` / `remove_label`** 单 label 原子改
- [x] **E8 · `exec_pod`** 批模式容器内执行

工具总数 70 → 79（`-9` deprecated `delete_*`/`bulk_*` 一对多迁移 + `+13` 新增 + `-5` 重复消除）。
**用户视角**: `diagnose_pod` 与 `cluster_health_snapshot` 互补（深度 vs 广度）；
`analyze_*` 与对应的 `create_*` 形成 write-verify 闭环。

---

## v0.5.0 收尾（2026-07-06）

**主题**：清掉 v0.4.x 标 @deprecated 的所有工具，工具面从 79 → 72；同步 OpenAPI cache 上限 + 文档全量刷新。

- [x] B1+B2 收尾：删 9 个 deprecated tool（5 `delete_*` + 4 `bulk_*`）
- [x] C2：OpenAPI cache 8 MiB cap + 修潜在 `OpenApiApi` 失效 bug
- [x] D1+D2：ROADMAP + CHANGELOG 同步
- [x] B5：README / tools-reference / tools.md / architecture.md 全部刷新
- [x] 测试侧：bulk + delete_low_risk 整套测试删除；caller-binding / write-safety / secret-discovery-diff 同步改用 `delete_resource`
- [x] `tests/test_tool_inventory.py`：断言 72 工具的总数与集合

最终工具数：**72 个**（含 `ping`）。`delete_resource` 单一路径覆盖所有 Kind 删除。

---

## v0.5.2 — 单步删除 + Prometheus 外部可达 URL（2026-07-07）

**主题**：删掉 v0.4.2 引入的 HMAC delete-token 二次确认（LLM-driven 场景里无效防护），同时修 `find_prometheus_service` 早就在用 NodePort 但 URL 一直返回 ClusterIP 的老 bug。

- [x] **F1 · `delete_resource` 单步化**
  - 移除 `confirm` / `confirmation_token` 两步流程
  - 移除 `K8S_MCP_DELETE_TOKEN_SECRET` + `K8S_MCP_DELETE_TOKEN_TTL_SECONDS` 配置
  - 移除 `enforce_write_safety()` 启动闸门（v0.4.2 的 `change-me` 字面默认值硬拒）
  - 移除 `safety.py` 里所有 token 辅助（`TokenError` / `issue_token` / `verify_token` / `make_delete_payload` / `assert_payload_matches` / `assert_caller_matches`）
  - 移除 `client.get_caller_identity()` 及 5 分钟 TTL 缓存
  - 守门完全交给 `K8S_MCP_READ_ONLY` + `K8S_MCP_NAMESPACE_ALLOWLIST`
- [x] **F2 · Prometheus 自动发现 URL 修正**
  - 新增 `_node_internal_ip()` + `_external_service_url()`
  - NodePort → `http://<first-node-internal-ip>:<nodePort>`
  - LoadBalancer → `http://<lb-ingress>:<port>`
  - ClusterIP 兜底 → `None`（让调用方退回 `_service_url` 老逻辑）
- [x] **F3 · 测试与文档**
  - `tests/test_delete.py`（7 测试）
  - `tests/test_prometheus.py` 新增 3 测试覆盖 NodePort / 兜底 / 硬编码候选
  - `tests/test_secret_audit.py` 删 caller_user / caller_uid 断言
  - 删除 `tests/test_caller_binding.py` / `tests/test_safety_delete.py` / `tests/test_write_safety.py`
  - CHANGELOG / README / env.md / tools-reference.md / tools.md / usage.md / examples.md / architecture.md 同步刷新

**威胁模型笔记**：在 LLM agent 既发起 preview 又发起 confirm 的场景里，
HMAC token 校验不构成任何额外防护——agent 完全可以一次性发两个调用。
v0.5.2 起删除完全靠 `READ_ONLY`（全局 kill switch）+ `NAMESPACE_ALLOWLIST`
（namespace 白名单 + cluster-scoped 一并拒）。如需更精细的"agent + 用户
双确认"，交给 agent 框架自己处理（如 Anthropic Computer Use 风格的
HITL），不再让工具层背锅。

---

**变更记录**
- 2026-07-05 初稿：从 plan 转成 checkbox 列表
- 2026-07-05 B2 完成：bulk_* → list variant，修复 2 处重复检查 bug，所有 23 个 bulk 测试通过
- 2026-07-06 v0.4.3 完成: Phase E 4+ 个分析器/diagnose/explain 工具 + search/add_label/remove_label/exec_pod，666 tests passing
- 2026-07-06 v0.5.0 完成: A1+A2 收尾 + B1+B2 收尾（删 9 个 deprecated）+ C2 OpenAPI cap + docs 刷新，655 tests passing
- 2026-07-07 v0.5.2 完成: 单步删除（删 HMAC token subsystem）+ Prometheus NodePort/LoadBalancer URL 修正，630 tests passing
- 2026-07-07 v0.5.3 完成: `top_pods` / `top_nodes` 三档级联 + `bootstrap_metrics_server` 显式工具，643 tests passing

---

## v0.5.3 — `top_pods` / `top_nodes` 三档级联 + 自动 bootstrap（2026-07-07）

**主题**：用户在 v0.5.2 发版验证里提了一个观察——"既然 Prometheus 已经在抓 cAdvisor + node-exporter，metrics-server 那条路径基本上是多余的。"把这观察落地为级联策略：让 `top_pods` / `top_nodes` 在 metrics-server 缺失时透明地落到 Prometheus，**仅在两条路径都断且有写权限时才尝试自动装 metrics-server**，避免 LLM 死磕 `bootstrap_metrics_server`。

- [x] **G1 · `top_pods` / `top_nodes` 三档级联**
  - Path 1: `/apis/metrics.k8s.io/v1beta1/...`（metrics-server，最快）
  - Path 2: Prometheus fallback（`container_cpu_usage_seconds_total[5m]` / `container_memory_working_set_bytes` for Pods；`node_cpu_seconds_total{mode!="idle"}[5m]` / `node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes` for Nodes）
  - Path 3: `bootstrap_metrics_server` 自动触发——仅当 1 + 2 都失败 **且** `K8S_MCP_READ_ONLY=false` **且** `kube-system` 在 `K8S_MCP_NAMESPACE_ALLOWLIST` 里
  - 三档全失败时 `RuntimeError` 字面列出 `bootstrap_metrics_server` / `find_prometheus_service()` / `prometheus_query(<PromQL>, prometheus_url=<URL>)` 三个备选——遵循「failure-path output must promote too」规则
- [x] **G2 · `bootstrap_metrics_server` 显式工具**
  - apply upstream `components.yaml` 到 `kube-system`
  - 默认 patch `--kubelet-insecure-tls`（自建集群 kubelet 自签证书场景）
  - 幂等：`Deployment/metrics-server` 已存在直接返回 `status=AlreadyInstalled` 不重 apply
  - 可覆盖 manifest URL：`K8S_MCP_METRICS_SERVER_MANIFEST_URL` 环境变量（离线 / 内网）
  - `K8S_MCP_NAMESPACE_ALLOWLIST` 不含 `kube-system` 时抛 `PermissionError`，避免误配置下静默写集群
- [x] **G3 · One-shot bootstrap gate**
  - `_BOOTSTRAP_ATTEMPTED` 模块级 flag：同一进程内 bootstrap 失败后**不再 retry**，避免 LLM 循环调用 `top_pods` 时反复 apply + probe 把 apiserver 打满
  - 重启 MCP server 自动重置（让运维改了 allowlist 后下一次进程能 retry）
- [x] **G4 · `label_selector` Prometheus 路径翻译**
  - cAdvisor 的 `pod` label 是 pod 名（不是 selector 驱动的），所以 Prometheus 路径需要先列一次 pod 拿名字再拼正则
  - `^...$` 锚定避免前缀误匹配
  - 找不到匹配 pod 时退回到 namespace-only filter + footer 提示，不抛异常
- [x] **G5 · 测试 + 文档**
  - `tests/test_metrics.py` 全量重写，15 测试覆盖三条路径 + label_selector 翻译 + bootstrap 一次性闸门 + bootstrap 工具本身的 5 个场景
  - `tests/test_tool_inventory.py`：`EXPECTED_TOOL_COUNT = 73`（含 `bootstrap_metrics_server`）
  - `docs/tools.md` 新增 `## top_pods / top_nodes 级联（metrics-server → Prometheus → bootstrap）` 段；Prometheus 段对 `top_pods` 的描述同步刷新
  - `docs/tools-reference.md` 工具总数更新；指标 / 监控段加 `bootstrap_metrics_server` 条目
  - `docs/env.md` 加 `K8S_MCP_METRICS_SERVER_MANIFEST_URL` 行（dev / 离线段）
  - `docs/CHANGELOG.md` 加 `[0.5.3]` 段（详尽列出 cascade + bootstrap 设计）

**威胁模型笔记**：自动 bootstrap 默认开启，**但**
`K8S_MCP_READ_ONLY=true` 或 `kube-system` 不在 allowlist 时**拒绝**——
运维默认不放开写权限时行为退回到"显式 `kubectl apply` 由人触发"。
One-shot gate 防止失控循环；agent 看到的最终错误就是直白的"请手动装
metrics-server / Prometheus"，不需要理解内部状态机。

---

**变更记录**
- 2026-07-05 初稿：从 plan 转成 checkbox 列表
- 2026-07-05 B2 完成：bulk_* → list variant，修复 2 处重复检查 bug，所有 23 个 bulk 测试通过
- 2026-07-06 v0.4.3 完成: Phase E 4+ 个分析器/diagnose/explain 工具 + search/add_label/remove_label/exec_pod，666 tests passing
- 2026-07-06 v0.5.0 完成: A1+A2 收尾 + B1+B2 收尾（删 9 个 deprecated）+ C2 OpenAPI cap + docs 刷新，655 tests passing
- 2026-07-07 v0.5.2 完成: 单步删除（删 HMAC token subsystem）+ Prometheus NodePort/LoadBalancer URL 修正，630 tests passing
- 2026-07-07 v0.5.3 完成: `top_pods` / `top_nodes` 三档级联 + `bootstrap_metrics_server` 显式工具，643 tests passing