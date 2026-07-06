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

**变更记录**
- 2026-07-05 初稿：从 plan 转成 checkbox 列表
- 2026-07-05 B2 完成：bulk_* → list variant，修复 2 处重复检查 bug，所有 23 个 bulk 测试通过
- 2026-07-06 v0.4.3 完成: Phase E 4+ 个分析器/diagnose/explain 工具 + search/add_label/remove_label/exec_pod，666 tests passing
- 2026-07-06 v0.5.0 完成: A1+A2 收尾 + B1+B2 收尾（删 9 个 deprecated）+ C2 OpenAPI cap + docs 刷新，655 tests passing