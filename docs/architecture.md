# 架构

## 目录结构

```text
src/k8s_mcp/
├── server.py         # FastMCP 入口，注册所有工具
├── config.py         # Settings（pydantic-settings，K8S_MCP_* env）
├── auth.py           # 三档认证（apiserver+token / kubeconfig / in-cluster）
├── client.py         # 缓存的 ApiClient 工厂
├── formatters.py     # YAML / Table / Describe + Secret 脱敏
├── safety.py         # RateLimit + ToolTimeout + SafeApiError 脱敏
└── tools/
    ├── generic.py    # list/get/get_yaml/describe/apply_yaml/replace/diff + label add/remove
    ├── workload.py   # create_deployment/statefulset/job/cronjob, scale/restart/set_image/set_resources
    ├── service.py    # create_service/ingress + expose_workload
    ├── logs.py       # get_pod_logs（长日志优化 + 多 Pod 并发）
    ├── pods.py       # list_pods + exec_pod（批模式容器内执行）
    ├── events.py     # list_events + get_events_for_object
    ├── configmap.py  # get/update configmap
    ├── delete_tool.py# delete_resource（单步；受 READ_ONLY + NS allowlist 守门）
    ├── metrics.py    # top_pods / top_nodes
    ├── rollout.py    # rollout_status / rollout_undo / rollout_history
    ├── node_ops.py   # cordon / uncordon / drain
    ├── wait_tool.py  # wait_resource（condition 或 JSONPath）
    ├── jsonpath.py   # get_resource_jsonpath
    ├── secret.py     # list_secrets + get_secret_value（单 key + reveal 审计）
    ├── discovery.py  # get_api_resources + explain_resource + find_images
    ├── autoscale.py  # create_hpa + create_pdb
    ├── rbac.py       # Role / RoleBinding / ClusterRole / ClusterRoleBinding + whoami + analyze_rbac
    ├── serviceaccount.py # create_serviceaccount
    ├── networkpolicy.py # create_networkpolicy + analyze_networkpolicy
    ├── storage.py    # create_pvc + bootstrap_local_path_provisioner + validate_pv_hostpath_paths
    ├── prometheus.py # prometheus_query / prometheus_query_range / pod_metrics
    ├── certs.py      # get_certificate_expiry（CRD + 内置 kind 都用 DynamicClient）
    ├── health.py     # cluster_health_snapshot（11 维集群体检）
    ├── cluster_info.py # cluster_info（apiserver / 版本 / 计数）
    ├── diagnostics.py   # diagnose_pod + diagnose_deployment（一键深度体检）
    ├── explain.py       # explain_pod（owner 链 + siblings + spec）
    ├── resource_usage.py # analyze_resource_usage（requests/limits 审计）
    ├── nvidia_gpu.py # NVIDIA GPU 节点、调度与 Operator 只读诊断
    ├── nvidia_metrics.py # Prometheus/DCGM GPU 指标发现与瞬时观测
    └── notifier.py   # notify 推送 webhook
}
```

`generic.py` 还额外暴露 `replace_resource`（PUT 带 ResourceVersion）、
`diff_resource`（apply 前预览差异）、`search_resources`（跨 kind 名字子串搜）、
`add_label` / `remove_label`（单 label 原子改，RFC 6901 转义）。

完整设计档案见 [PLAN.md](./PLAN.md)（archived），当前路线图见
[ROADMAP.md](./ROADMAP.md)，用法示例见 [tests/](../tests/)。

## 设计要点

### 工具模块独立性

每个 `tools/*.py` 模块暴露一个 `register(mcp)` 函数。新增工具模块只要在
`server.py` 的 `_register_tools` 里 import + 调用一次，**不需要**改其他模块。
91 个工具的注册入口集中在一处，新增模块不会让 `server.py` 增长太多。

### 配置 + 守门分层

- **认证**（`auth.py` + `client.py`）：三档自动探测，缓存 ApiClient。
- **守门**（`config.Settings` + 各 tool 内的 `_read_only_guard` / `_ensure_ns`）：
  写工具调工具前先过两层。**`read_only`** 全局拒绝；**`namespace_allowlist`**
  按目标 namespace 校验。
- **删除守门**：v0.5.2 起删除是**单步**——无 preview / 无 token 二次确认。守门由
  `K8S_MCP_READ_ONLY`（全局 kill switch）+ `K8S_MCP_NAMESPACE_ALLOWLIST`（namespace 白名单）承担。
  原来的两步 HMAC 流程在 LLM-driven 场景里没有防护意义（agent 自己就能
  发 preview + confirm 两个调用），故 v0.5.2 移除。

### 为什么是 stdio 而不是 HTTP/SSE？

v1 只走 stdio。理由：

- 一个 MCP server 实例 = 一个 kubectl 上下文，stdio 跟 LLM Agent 的 client-server
  拓扑天然 1:1。
- stdio 没有端口冲突、没有 firewall 顾虑，部署到 LLM Agent 客户端就是改个 JSON。
- HTTP / SSE 是 v2 的事。

### 进程内状态（重启会丢）

MCP server 是单进程的，3 处 in-memory 缓存：

- **apiserver ApiClient** —— 跟 settings 的 auth 字段绑定。auth 字段变就重建。
- **Prometheus 候选 Service 列表** —— 走 stale-on-error，失败重扫。
- **Token bucket（rate limit）** —— 进程内，per-tool。重启即重置。

LLM Agent（Cherry Studio / Claude Desktop）的 UI 重启**不会**重启 MCP server；
要看 MCP server 是不是在跑新代码，**MCP 客户端连接重连**（删了再加）即可。

### 测试策略

630 个测试覆盖所有写 / 读 / 守门路径。模式：

- **mock ApiClient** —— 在 tool 模块级别 monkeypatch `_core_v1` / `_apps_v1` 等
  为 recording fake，捕获调用 + 模拟 404 / Forbidden。
- **mock DynamicClient** —— `find_images` 这类走 DynamicClient 的工具
  在 `generic._dyn_client` / `generic._resource_for_kind` 边界 patch。
- **lint** —— `uv run ruff check src tests`（pyproject 配的 `E F W I N UP B`）。

`uv run pytest` 一条命令全过；CI（`.github/workflows/ci.yml`）跑
Python 3.11 / 3.12 / 3.13 lint + test 矩阵。
