# Kubernetes MCP 服务 — 设计规划

> 与 Claude Code 共享的项目级设计文档。修改时请保持与 `~/.claude/plans/` 中最新 plan 文件的一致性。

## 1. Context & Goals

构建一个面向 LLM Agent 的 Kubernetes MCP 服务，让 Claude/Cursor/Cline 等客户端通过自然语言完成日常 K8s 运维：**查看资源（读）、创建/修改资源（写）、查看 Pod 日志、查看任意资源 YAML**。Delete 作为高危命令需要二次确认流程。

为什么是 MCP 而不是 `kubectl` 包装：MCP 提供结构化工具调用与类型 schema，Agent 能精确传参并拿到格式化结果，比解析 CLI 文本更稳定；一次开发可对接所有 MCP 客户端。

**工作目录**：`/Users/mayc/codes/k8s-mcp/`（空目录，从零起步）

## 2. Tech Stack

| 维度 | 选择 |
|---|---|
| 语言 | Python 3.11+ |
| MCP 框架 | `mcp[cli]` (官方 Python SDK, FastMCP 风格) |
| K8s 客户端 | `kubernetes`（官方 python-client）+ `kr8s`（DynamicClient 的替代，更轻） |
| 配置 | `pydantic-settings` |
| YAML | `pyyaml` |
| 包管理 | `uv` |
| 传输 | stdio（MCP 默认，Claude Desktop/Cursor 直连） |
| 测试 | `pytest` + 自建 fake ApiClient |

`pyproject.toml` 依赖：
```
mcp[cli]>=1.0
kubernetes>=29.0
kr8s>=0.12.0          # 替代 DynamicClient
pydantic>=2.0
pydantic-settings>=2.0
pyyaml>=6.0
```

## 3. 认证模式（**三档自动切换**）

### 模式 A：apiserver URL + token（**新增**，远程管控/CI/CD）
通过环境变量或 CLI 参数显式提供：
```bash
export K8S_API_SERVER=https://api.example.com:6443
export K8S_API_TOKEN=eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9...
export K8S_API_CA_CERT=/path/to/ca.crt   # 可选；不提供则走系统 CA
export K8S_API_INSECURE=false            # 可选；true 时跳过 TLS 校验（仅测试）
```

### 模式 B：kubeconfig（**默认**，本地开发）
```bash
export KUBECONFIG=~/.kube/config        # 可选；缺省时 SDK 自动找 ~/.kube/config
# 多 context 时可指定：
export K8S_KUBE_CONTEXT=my-cluster
```

### 模式 C：in-cluster（**自动检测**，部署在 K8s Pod 内）
检测到 `/var/run/secrets/kubernetes.io/serviceaccount/token` 时自动使用。

### 优先级
1. 若 `K8S_API_SERVER` **和** `K8S_API_TOKEN` 都设置 → 模式 A
2. 否则若 `KUBECONFIG` 或 `~/.kube/config` 存在 → 模式 B
3. 否则若检测到 in-cluster 标识 → 模式 C
4. 否则抛出明确错误（提示用户设置环境变量或准备 kubeconfig）

CLI 参数优先级高于环境变量：`--api-server`、`--token`、`--kubeconfig`、`--kube-context`。

## 4. 项目结构

```
k8s-mcp/
├── PLAN.md                   # 本文件
├── README.md
├── pyproject.toml
├── .python-version
├── .env.example              # 环境变量示例
├── src/k8s_mcp/
│   ├── __init__.py
│   ├── server.py             # FastMCP 入口，mcp.run()
│   ├── config.py             # Settings
│   ├── auth.py               # 三档认证加载
│   ├── client.py             # ApiClient 工厂（lru_cache）
│   ├── formatters.py         # YAML / Table / Describe 输出
│   ├── safety.py             # 删除二次确认 + namespace allowlist + 敏感字段脱敏
│   └── tools/
│       ├── __init__.py
│       ├── generic.py        # list/get/get_yaml/describe/apply
│       ├── workload.py       # create_deployment/statefulset, scale/restart/set_image
│       ├── service.py        # create_service/ingress, expose_workload
│       ├── logs.py           # get_pod_logs
│       ├── pods.py           # list_pods / 二次确认式 delete_resource
│       ├── configmap.py      # get/update configmap (注：与 config.py 改名避免冲突)
│       └── events.py         # list_events
└── tests/
    ├── conftest.py           # fake ApiClient fixture
    ├── test_auth.py
    ├── test_safety.py
    ├── test_generic.py
    ├── test_workload.py
    ├── test_service.py
    └── test_logs.py
```

## 5. Tool 清单（CRUD + 日志 + 排障）

> **删除（Delete）**：单独处理，见 §6。所有工具一律走 `apply_yaml` 的 Kind 用泛型 `delete_resource`（二次确认）。

### 5.1 通用资源（5 个）
| Tool | 等级 | 用途 |
|---|---|---|
| `list_resources` | SAFE | 任意 Kind 列表 |
| `get_resource` | SAFE | 任意 Kind 详情（JSON） |
| `get_resource_yaml` | SAFE | 任意 Kind YAML（首选排查入口） |
| `describe_resource` | SAFE | 类 kubectl describe 文本 |
| `apply_yaml` | CAUTION | 应用 YAML（多 doc 支持）；工具描述中要求先 `get_resource_yaml` 核对 |

### 5.2 Workload（5 个）
| Tool | 等级 | 用途 |
|---|---|---|
| `create_deployment` | CAUTION | 友好参数拼 Deployment manifest |
| `create_statefulset` | CAUTION | 友好参数拼 StatefulSet manifest |
| `scale_workload` | CAUTION | Deployment/StatefulSet 调整副本数 |
| `restart_workload` | CAUTION | rollout restart（annotation patch） |
| `set_image` | CAUTION | 单容器镜像更新 |

### 5.3 Service & 路由（3 个）
| Tool | 等级 | 用途 |
|---|---|---|
| `create_service` | CAUTION | 拼 Service manifest |
| `create_ingress` | CAUTION | 拼 Ingress manifest |
| `expose_workload` | CAUTION | 一键把已有 workload 暴露成 Service |

### 5.4 日志与排障（3 个）
| Tool | 等级 | 用途 |
|---|---|---|
| `get_pod_logs` | SAFE | 单/多 Pod 日志，支持 tail/since/previous/timestamps |
| `list_pods` | SAFE | Pod 列表，可 label/field selector |
| `list_events` | SAFE | 事件列表，Warning 优先 |

### 5.5 配置（2 个）
| Tool | 等级 | 用途 |
|---|---|---|
| `get_configmap` | SAFE | 读 ConfigMap |
| `update_configmap` | CAUTION | 替换 ConfigMap.data（整个对象替换，不做差分合并） |

**Secret 不暴露工具**。Secret 的查看/修改需求建议走 `get_resource_yaml` / `apply_yaml` 通用通道，并在 `safety.py` 中对 Secret 的 value 字段做脱敏（默认 `***`，显式 `reveal=True` 才输出原文，且工具描述中警告）。

合计 **18 个工具**。

## 6. 删除的二次确认机制

删除是高危操作，**禁止一次调用就执行**。采用 **preview → confirm** 两步走：

### 6.1 工具签名
```python
@mcp.tool(
    description="""DANGEROUS: Delete a Kubernetes resource. This is IRREVERSIBLE.

Workflow:
  1. Call with confirm=False (default). The tool returns a YAML preview of the
     resource that would be deleted and a confirmation_token.
  2. Show the preview to the user and get explicit verbal confirmation.
  3. Re-call with confirm=True and the same confirmation_token to execute.

NEVER call with confirm=True without showing the preview to the user first.
"""
)
def delete_resource(
    kind: str,
    name: str,
    namespace: str | None = None,
    confirm: bool = False,
    grace_period_seconds: int = 30,
) -> dict:
    ...
```

### 6.2 confirm=False 时行为
- 调用 `get_resource_yaml(kind, name, namespace)` 拿到当前状态
- 生成 5 分钟有效的 `confirmation_token`（HMAC 签名 payload，包含 kind/name/namespace/grace_period）
- 返回：
  ```json
  {
    "preview_yaml": "...",
    "confirmation_token": "eyJhbGciOiJIUzI1NiJ9...",
    "expires_in_seconds": 300,
    "instruction": "Show the preview to the user. After their approval, re-call with confirm=True and the confirmation_token above."
  }
  ```

### 6.3 confirm=True 时行为
- 校验 `confirmation_token`：未过期、签名匹配、payload 与参数一致
- 校验失败 → 报错并要求重新走 confirm=False 流程
- 校验通过 → 调 CoreV1Api/AppsV1Api delete，foreground / grace_period_seconds 按入参
- 返回删除结果（含 resourceVersion、删除时间）

### 6.4 Agent 必须遵守的协议
工具描述里强制写：
> Always call with `confirm=False` first, show the YAML preview to the user, get explicit confirmation, then re-call with `confirm=True` and the `confirmation_token`.

## 7. 安全模块 `safety.py`

| 机制 | 行为 |
|---|---|
| `--read-only` | 所有 CAUTION/DANGEROUS 工具抛 `ToolError` |
| `--namespace-allowlist ns1,ns2` | 写工具执行前校验目标 ns（不在白名单 → 拒绝） |
| Secret value 脱敏 | `get_resource_yaml` 对 Secret 默认 `***`，需 `reveal=True` 才返回 |
| Delete 二次确认 | §6 |
| `apply_yaml` 提醒 | 工具描述要求先 `get_resource_yaml` |

## 8. 配置（环境变量 + CLI）

`config.py`（pydantic-settings）：
```python
class Settings(BaseSettings):
    # Auth（mode A）
    api_server: str | None = None
    api_token: str | None = None
    api_ca_cert: str | None = None
    api_insecure: bool = False

    # Auth（mode B）
    kubeconfig: str | None = None
    kube_context: str | None = None

    # 安全
    read_only: bool = False
    namespace_allowlist: list[str] | None = None
    delete_token_secret: str = "<random>"   # HMAC 密钥；生产建议通过环境注入

    # 日志/输出
    log_level: str = "INFO"
    default_tail_lines: int = 100
```

所有环境变量以 `K8S_MCP_` 前缀，例如 `K8S_MCP_READ_ONLY=true`。

`.env.example`：
```
K8S_MCP_LOG_LEVEL=INFO
K8S_MCP_DEFAULT_TAIL_LINES=100

# Auth mode A
K8S_MCP_API_SERVER=
K8S_MCP_API_TOKEN=
K8S_MCP_API_CA_CERT=

# Auth mode B
K8S_MCP_KUBECONFIG=
K8S_MCP_KUBE_CONTEXT=

# Safety
K8S_MCP_READ_ONLY=false
K8S_MCP_NAMESPACE_ALLOWLIST=
K8S_MCP_DELETE_TOKEN_SECRET=change-me
```

## 9. 实施步骤（推荐顺序）

1. **脚手架** — `uv init`、依赖、`hello world` FastMCP 跑通
2. **认证** — `auth.py` 三档切换，单测覆盖优先级
3. **Settings & Client** — `config.py`、`client.py`、lru_cache
4. **通用只读** — `list/get/get_yaml/describe`
5. **日志与事件** — `get_pod_logs`、`list_events`、`list_pods`
6. **apply** — `apply_yaml`（含多 doc 拆分）
7. **友好创建** — `create_deployment/service/ingress`、`expose_workload`
8. **运维 API** — `scale/restart/set_image`、`update_configmap`
9. **删除二次确认** — `safety.py` + `delete_resource` + token 校验
10. **测试** — 单测 + 集成
11. **README & Claude Desktop 配置示例**

## 10. 验证方案

### 10.1 单测
- `test_auth.py`：三档切换、优先级、错误路径
- `test_safety.py`：read-only、namespace allowlist、token 过期/篡改
- `test_generic.py`：mock ApiClient，list/get/get_yaml/apply
- `test_workload.py` / `test_service.py` / `test_logs.py`：happy path + 错误

### 10.2 集成（手工）
1. `kind create cluster --name k8s-mcp-test`
2. `export KUBECONFIG=~/.kube/kind-config`
3. `uv run k8s-mcp`
4. 配置 Claude Desktop `claude_desktop_config.json`：
   ```json
   {
     "mcpServers": {
       "k8s": {
         "command": "uv",
         "args": ["--directory", "/Users/mayc/codes/k8s-mcp", "run", "python", "-m", "k8s_mcp"]
       }
     }
   }
   ```
5. 端到端冒烟（用 Claude 对话）：
   - "列出 default namespace 的 Pod"
   - "部署 nginx:1.25 Deployment，3 副本，暴露 80"
   - "给它加一个 ClusterIP Service + Ingress"
   - "给我看那个 Deployment 的 YAML"
   - "看 nginx pod 最近 50 行日志"
   - "镜像升到 nginx:1.27"
   - **"删除那个 Deployment"** — 验证二次确认：先返回 preview，用户确认后才真删
6. 模式 A 验证：用 `kubectl create token` 生成 token，配 env vars 直连远程集群
7. 安全验证：
   - `--read-only` → 所有写工具报错
   - `--namespace-allowlist default` → 对其他 ns 的写操作拒绝

### 10.3 失败判定
- 任何工具调用抛未捕获异常 → fail
- `read-only` 模式下写工具未拒绝 → fail
- 命名空间越权写入未拦截 → fail
- 删除未走二次确认 → fail
- Secret value 未脱敏 → fail

## 11. Out of Scope（v2+）

- CRD 通用适配（用 `apply_yaml` / `list_resources` 兜底，足够 90% 场景）
- `exec_pod`、`port_forward`
- Helm / Kustomize 集成
- 多集群路由
- MCP HTTP/SSE 传输（v1 只走 stdio）
- Docker 镜像 / Helm chart 发布
- RBAC 工具（list/grant role）

---

## 12. Drift Log（plan ↔ code 偏差记录）

每次发现 plan 与 code 不一致、或 plan 描述已过时、需在这里登记一行。
新功能 / 重构也要在这里追一笔，让 plan 文档保持诚实。

### 12.1 实现期漂移

- 2026-07-05: plan §2 tech stack 列出 `kr8s`，但代码从一开始就只用
  `kubernetes`（DynamicClient）；`kr8s` 从未被引入。已删 `kr8s` 描述。
- 2026-07-05: §3 描述 `crud` 一档，但实际认证"三档"是
  apiserver+token / kubeconfig / in-cluster；§2 表格里"CRUD"是工具
  维度、§3 描述的是 auth 维度，混在一起容易读错。auth 三档以
  `auth.py` 为准。
- 2026-07-05: §10.1 提到 "crud 操作" 一档，但实际实现里写操作覆盖了
  workload / storage / RBAC / networking 等多档；这里按工具维度重新
  分组更清楚。
- 2026-07-05: `apply_yaml` 内部新增 `_apply_yaml_records()` 返回结构化
  per-doc 结果（用于 `bulk_*`），但外部 `apply_yaml` 字符串格式
  `"kind/name: action"` 保持向后兼容——plan §4 仅描述了字符串版。
- 2026-07-05: 新增 `formatters.format_age` / `format_relative_time`
  作为唯一时间渲染来源（取代散落在 generic / health / pods / secret
  / events 5 个模块的 `_age` / `_rel_time` / `_format_time` 副本）。
- 2026-07-05: `cluster_health_snapshot` 之前有 4 处重复 list_pod 调用
  已合并为 1 次 top-level fetch；plan §5 没具体到 sub-section 级
  优化，仅描述"独立出错边界"。
- 2026-07-05: `client.py` 现在给 Configuration 设 5s connect / 30s
  read timeout，plan §3 没有涉及——之前 python-client 默认无限，
  实测半死 apiserver 会让 MCP 工具调用挂死。
- 2026-07-05: `_api_version_for` 现在从 `generic.py` 单源导出，
  `wait_tool.py` 不再持有副本。
- 2026-07-05: 所有 tool 模块的 `import yaml` / `import copy` /
  `import re` / `import os` 全部上移到文件顶部（之前散落在
  函数体内）。
- 2026-07-05: server-managed metadata key 列表（`_MANAGED_METADATA_KEYS`
  vs `server_managed_metadata`）在 `generic.py` 内部去重为
  `_SERVER_MANAGED_METADATA_KEYS` + `_YAML_NOISE_METADATA_KEYS` 两个
  常量，行为不变。

### 12.2 待决 / 待跟踪

- `set_resources` 在 read_only=False 且 `K8S_MCP_DELETE_TOKEN_SECRET`
  仍为默认值时，启动日志会 SECURITY-warn，但**不**强制 fail——
  用户接受这个折中（部署方可忽略 WARN）。如果后续想升级为 hard
  fail（启动拒绝），需要明确开关。
- Prometheus discovery 的 wide-scan fallback 在非常大的集群里仍然
  可能耗时长（list_service_for_all_namespaces + filter），目前没有
  二次 timeout 保护。需要观察生产环境再决定是否加 hard ceiling。

---

**变更记录**
- 2026-07-02 初稿：Python + FastMCP，CRUD（删走二次确认），三档认证（apiserver+token / kubeconfig / in-cluster）
- 2026-07-05 增 §12 Drift Log：把代码已经偏离 plan 的地方登记在此；后续发现 / 重构都要追加