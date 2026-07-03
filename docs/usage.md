# 程序化调用（无需 MCP server）

每个注册到 FastMCP 的工具同时也是 `k8s_mcp.tools.*` 下的纯 Python 函数，
所以你可以在脚本、notebook 或 CLI 里直接调，**不用启 MCP server**。认证、
安全、namespace allowlist 都仍然生效——它们住在 `config`、`safety` 和
各 tool 的内部检查里，不在 MCP 层。

```python
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

## 适用场景

- **CI / CD 流水线** —— Python step 里直接调 k8s-mcp 的 tool，不起 MCP server。
- **notebook** —— Jupyter 里调 `cluster_health_snapshot()` 看集群状态。
- **ad-hoc 脚本** —— 不写一堆 `subprocess.run(["kubectl", ...])`，
  直接 `from k8s_mcp.tools import generic; generic.list_resources(...)`。
- **agent 框架二次开发** —— 自己写 Agent loop，工具列表直接复用 k8s-mcp 的
  Python 函数 + docstring，docstring 就是 agent 看得到的工具描述。
