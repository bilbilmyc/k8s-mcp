# exec_pod 设计提案（待签）

> 状态：**DRAFT / 待用户签字** — F4
> 目的：在 `src/k8s_mcp/tools/` 下加 `exec_pod` 工具，让 LLM Agent 能跑 `kubectl exec` 等价的命令。

---

## 1. 为什么不直接做

WebSocket exec 比想象中麻烦：

- K8s exec API 是 **WebSocket + 多 channel** 协议（v4.channel.k8s.io）：
  - channel 0 = stdin / 1 = stdout / 2 = stderr / 3 = error（带 exit code）/ 4 = resize（TTY）
- `kubernetes.stream.stream()` 高级 helper **只返回 stdout**，没有 exit code、没有 stderr 单独通道
- 要拿 exit code 必须用底层 `WSClient`，自己管理 channel read loop + timeout
- shell-injection vs argv 的语义要决定（`kubectl exec pod -- ls -la` 走 argv 不走 shell）
- 多容器 Pod 必须显式选 container（kubectl 默认拿第一个，pod 多容器会歧义）

加上安全含义（容器里跑任意命令 = privilege escalation risk），建议 **先定 shape 再写**。

---

## 2. 三档候选

### A. 极简单 stdout（`stream()` 走默认）

```python
def exec_pod(pod, command, namespace="default", container=None, timeout=30) -> str:
    """Returns combined stdout. No exit code, no separate stderr."""
```

- **优点**：50 行实现，1 天交付，纯 Python，零新依赖
- **缺点**：
  - 没 exit code → Agent 没法判断命令是否成功（`grep foo` miss 时也返回 0）
  - stderr 混进 stdout → 排障混乱
  - 超时只能靠 `_request_timeout`，没精确 wall-clock 控制
- **适用**：demo / 玩具；不推荐生产

### B. 完整批模式（WSClient 底层）

```python
def exec_pod(pod, command, namespace="default", container=None, timeout=30) -> str:
    """Returns: $ <cmd>\n<stdout>\n<stderr>\n(exit code: <N>)"""
```

- **优点**：exit code + stderr 分离 + 精确超时 + 行为贴近 `kubectl exec`
- **缺点**：~150 行，需要写 channel read loop、timeout 线程、错误处理
- **适用**：✅ 推荐 — 与本仓库其他工具的设计深度匹配

### C. shell out 到 `kubectl`

```python
subprocess.run(["kubectl", "exec", pod, "-n", ns, "-c", c, "--", *command],
               capture_output=True, text=True, timeout=30)
```

- **优点**：50 行；零 WebSocket 复杂度；exit code / stderr / stdout 全部免费
- **缺点**：
  - 需要 MCP server 宿主上有 `kubectl` 二进制（**新运行时依赖**）
  - `kube_context` 不一定能跟 MCP server 当前的认证模式对上
  - 跟仓库其他工具"全部走 kubernetes Python client"的风格不一致
  - 之前 port-forward 因为 IPv6 反复卡死被砍过；同样宿主依赖坑
- **适用**：作为 B 实现遇阻的 fallback，不推荐首选

---

## 3. 推荐：B 档完整批模式

### 3.1 函数签名

```python
def exec_pod(
    pod_name: str,
    command: list[str],
    namespace: str | None = "default",
    container: str | None = None,
    timeout_seconds: int = 30,
) -> str
```

### 3.2 返回格式

```
$ ls -la /tmp
total 12
drwxr-xrwt 3 root root 4096 Jul  6 10:30 .
drwxr-xr-x 1 root root 4096 Jul  6 10:25 ..
-rw-r--r-- 1 root root  123 Jul  6 10:25 app.log
(exit code: 0)
```

非零 / 超时 / 错误时：

```
$ false
(exit code: 1)
```

```
$ sleep 60
❌ exec timeout after 30s (pod may still be running the command)
```

### 3.3 安全 / 守门（跟现有写工具一致）

- `K8S_MCP_READ_ONLY=true` → 拒收（PermissionError）
- `K8S_MCP_NAMESPACE_ALLOWLIST` 不含目标 ns → 拒收
- 不做命令白名单（信任 K8s RBAC；用户能 pods/exec 就能跑任意命令）

### 3.4 局限（明确文档化）

- **不是交互式 shell**：`command` 是 argv list，不走 shell；要 `pipe` / `redirect` 必须显式 `["sh", "-c", "..."]`
- **无 streaming 输出**：命令跑完才返回；不能 `tail -f`
- **超时是 wall-clock**：超时不取消 pod 里的命令（K8s exec 协议没 cancel），只是断开 WebSocket

### 3.5 测试覆盖

- happy path：返回 stdout + exit code 0
- non-zero exit：返回 exit code 1
- timeout：返回友好错误
- 容器未指定 + 单容器 pod：自动选第一个
- 多容器 + 未指定：报错，列出可选
- read_only 拒收
- namespace_allowlist 拒收
- Pod 不存在：报错

### 3.6 工作量估计

- 实现：B 档 ~150 行代码
- 测试：~150 行（覆盖上面 7 类）
- 文档：`tools-reference.md` + `tools.md` 各自一段
- 总计：**2-3 天**

---

## 4. 决定

要不要做、做哪一档？

| 选项 | 工作量 | 风险 | 推荐 |
|---|---|---|---|
| **B 档完整批模式** | 2-3 天 | 中（WebSocket） | ✅ |
| A 档极简单 stdout | 1 天 | 低 | 玩具 |
| C 档 shell kubectl | 1 天 | 中（新依赖） | 不推荐 |
| 推迟 F4，先做 F5 analyze_rbac | 3-5 天 | 低 | 备选 |

> 决定后改本文档为 ✅ / ❌，再进实现。
