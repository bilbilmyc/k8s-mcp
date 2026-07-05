# Roadmap

发版流程（PyPI OIDC + GitHub Actions + 6 个 Release）就位后，下一步是项目本身的健壮性、性能和 LLM 友好度。
每个 Phase 一个独立 PR，按下面勾选状态跟踪。

> 本文件与 `~/.claude/plans/` 下的最新 plan 文件保持一致；任何变更先改 plan 再改这里。

---

## Phase A — 快速止血

bug 性质，影响所有用了 notify 或 cluster_health_snapshot 的部署。

- [ ] **A1 · notifier 健壮性** — `src/k8s_mcp/tools/notifier.py`
  - [ ] retry 3 次，指数退避（0.5s / 1s / 2s），覆盖 500/502/503/504
  - [ ] payload 大小 guard：Slack 40KB / WeCom 4KB / Feishu 30KB，超长时附 `⚠️ truncated from N bytes`
  - [ ] `requests.Session()` 模块级连接池
  - [ ] 错误消息统一英文（保留中文在 docstring 里）
  - [ ] `tests/test_notifier.py`：mock retry + 截断 + 超时
  - PR: TBD

- [ ] **A2 · health.py N+1 修复** — `src/k8s_mcp/tools/health.py`
  - [ ] `_section_workloads` 改用 `list_*_for_all_namespaces` + 客户端按 `nss` 过滤
  - [ ] `tests/test_health.py`：断言 `list_*_for_all_namespaces` 只调用 1 次
  - 目标：50-ns 集群 `cluster_health_snapshot` 从 ~30s 降到 ~5s
  - PR: TBD

---

## Phase B — 工具整合（LLM 友好度）

LLM 选工具靠 description；当前 56 个工具里有 ~18 个是 wrapper，重叠严重。

- [ ] **B1 · deprecate kind-specific `delete_*`**
  - `delete_pod`（`pods.py`）→ `delete_resource(kind="Pod")`
  - `delete_service` / `delete_ingress`（`service.py`）→ `delete_resource(kind=...)`
  - `delete_configmap`（`configmap.py`）→ `delete_resource(kind=...)`
  - `delete_pvc`（`storage.py`）→ `delete_resource(kind=...)`
  - docstring 标 `@deprecated`，v0.5.0 删除

- [x] **B2 · `bulk_*` → 单工具列表变体** — PR: #12
  - [x] `scale_workload(name: str | list[str], ...)` 已支持列表，`restart_workload` / `set_image` / `delete_pvc` 同理
  - [x] `bulk_scale` / `bulk_restart` / `bulk_set_image` / `bulk_delete_pvc` 已标 @deprecated
  - [x] 修复 `bulk.py` 中重复的 label_selector 检查 bug（2 处）

- [ ] **B3 · `list_pods` vs `list_resources(kind="Pod")` 边界澄清**

- [ ] **B4 · 6 对工具的 description 边界文案**
  - `top_pods` / `top_nodes` / `pod_metrics`
  - `restart_workload` / `rollout_undo`
  - `apply_yaml` / `replace_resource` / `diff_resource`
  - `wait_resource` / `cluster_health_snapshot`
  - `bulk_*` / 单工具（配合 B2）
  - `cluster_info` / `cluster_health_snapshot`

- [ ] **B5 · 工具数修正 + inventory 测试**
  - README.md / README.en.md：70 → 38
  - `docs/tools-reference.md` 同步
  - 新增 `tests/test_tool_inventory.py`：断言工具数在 [36, 40]

---

## Phase C — 性能 / 资源

深水区，影响频繁调用 Prometheus / 多 pod 日志 / 大 CRD 集群。

- [ ] **C1 · 连接池 + 超时分级**
  - 新增 `src/k8s_mcp/_http.py`：`shared_requests_session()` (thread-local, pool_connections=10)
  - `prometheus._prom_get` 改用 session
  - `logs._fetch_logs_multi` 多 pod 改 `ThreadPoolExecutor(max_workers=8)` 并发
  - `_PROM_HTTP_TIMEOUT` 拆 `(5, 30)` connect/read

- [ ] **C2 · OpenAPI cache 大小上限** — `discovery.py`
  - `_OPENAPI_CACHE_MAX_BYTES = 8 MiB`，超 cap 清空强制下次重读

- [ ] **C3 · auth.py 双路径去重** — `auth.py`
  - 抽 `_default_kubeconfig_path()` helper，line 74-87 和 122-126 共用

---

## Phase D — 文档落地

- [ ] **D1 · 本文件（ROADMAP.md）** ← 当前 PR
- [ ] **D2 · CHANGELOG [Unreleased]** — Phase A/B/C 摘要

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
- `exec_pod` / log streaming
- RBAC 工具（list/grant role）
- Docker image / Helm chart 发布
- Slack 签名验证 / 端到端 idempotency 协议

---

**变更记录**
- 2026-07-05 初稿：从 plan 转成 checkbox 列表
- 2026-07-05 B2 完成：bulk_* → list variant，修复 2 处重复检查 bug，所有 23 个 bulk 测试通过