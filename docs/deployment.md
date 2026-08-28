# 部署与 RBAC

[English](./deployment.en.md) · [返回文档中心](./README.md)

## 推荐拓扑

| 环境 | MCP 配置 | Kubernetes 身份 |
| --- | --- | --- |
| 开发/日常运维 | 默认策略（无需设置 `READ_ONLY=false`） | 具备所需写权限的受限身份 |
| staging 受限写入 | `NAMESPACE_ALLOWLIST=staging` | 仅 `staging` 的 RoleBinding |
| 审计/诊断 | `READ_ONLY=true` | 只读 ServiceAccount 或个人只读 kubeconfig |

## 快速部署模板

```bash
kubectl apply -f deploy/rbac/read-only.yaml
# 将 namespace-operator.yaml 的 <namespace> 替换成实际值后再应用
kubectl apply -f deploy/rbac/namespace-operator.yaml
```

- [read-only.yaml](../deploy/rbac/read-only.yaml) 创建 ServiceAccount 并绑定 Kubernetes 内置 `view` ClusterRole。
- [namespace-operator.yaml](../deploy/rbac/namespace-operator.yaml) 是示例性命名空间写权限；请删除不需要的资源和 verbs，再替换 `<namespace>`。

> [!CAUTION]
> 模板是起点，不是“万能生产权限”。不要把 `cluster-admin` 绑定给 MCP ServiceAccount，也不要为了通过一次 `Forbidden` 就扩大到 `*`。

## 运行位置与连接方式

k8s-mcp 目前**只支持 stdio transport**：MCP 客户端（Claude Desktop、Cursor 等）把 `k8s-mcp serve` 作为**本地子进程**启动，server 与客户端同机运行，再通过网络凭据访问 Kubernetes API。因此不存在"把 server 跑成集群里的 Pod、远程 MCP 客户端直连"的部署形态——stdio 要求子进程在客户端机器上。远程 HTTP transport 在[路线图](./ROADMAP.md)的 v2+ 清单中，落地前还需要解决传输层认证、TLS、网络策略、审计和请求大小限制。

按"server 跑在哪、用什么身份连集群"，有两种受支持的组合：

### 方式一：本地 kubeconfig（个人开发/运维默认）

Server 与你的 `kubectl` 用同一份 kubeconfig，权限即你个人的 RBAC 身份：

```bash
export KUBECONFIG="$HOME/.kube/config"
export K8S_MCP_KUBE_CONTEXT=my-cluster   # 多 context 时可选
k8s-mcp serve
```

### 方式二：本地运行 + ServiceAccount token 直连 apiserver（共享/受控身份）

用 `deploy/rbac/` 里的模板创建受限 ServiceAccount，然后签发 token，让本地 server 以**集群侧最小权限**身份运行——适合把身份收敛成专用账号而不是复用个人 kubeconfig：

```bash
# 1. 创建受限 ServiceAccount（见上文模板）
kubectl apply -f deploy/rbac/read-only.yaml

# 2. 签发有时效的 token 并交给 server（认证模式 A）
export K8S_MCP_API_SERVER="https://api.example.com:6443"
export K8S_MCP_API_TOKEN="$(kubectl -n ops create token k8s-mcp-reader --duration=8h)"
export K8S_MCP_API_CA_CERT="$HOME/.kube/ca.crt"   # 自签 CA 时必需
k8s-mcp serve
```

token 有时效，过期后需重新签发；不要把它写进客户端配置文件或代码仓库。

### 方式三：in-cluster sidecar（Agent 容器内启动子进程）

stdio server 必须由同一容器内的 MCP 客户端作为子进程启动。把 `k8s-mcp` 打进 Agent 镜像后，可以用下面的 Pod 片段在集群内运行完整的 Agent + MCP 组合——它是客户端在容器内的子进程，不是可独立访问的远程服务：

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: your-agent
  namespace: ops
spec:
  serviceAccountName: k8s-mcp-reader
  containers:
    - name: agent
      image: your-registry/agent-with-k8s-mcp:tag
      env:
        - name: K8S_MCP_READ_ONLY
          value: "true"
```

客户端在该容器内用 `command: k8s-mcp` 启动子进程。若要独立远程部署，需先实现远程 MCP transport，并补齐认证、TLS、网络策略、审计和请求大小限制；本仓库当前只提供 stdio。

## 上线前检查

- [ ] 只读实例显式设置了 `READ_ONLY=true`。
- [ ] 写实例有精确 namespace allowlist。
- [ ] 写入 RBAC 不含不必要的 secrets、RBAC 管理或 cluster-scoped 权限。
- [ ] webhook 使用 HTTPS 与精确 host allowlist。
- [ ] 审计日志可追踪该 ServiceAccount。
