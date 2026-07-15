# 部署与 RBAC

[English](./deployment.en.md) · [返回文档中心](./README.md)

## 推荐拓扑

| 环境 | MCP 配置 | Kubernetes 身份 |
| --- | --- | --- |
| 开发/日常运维 | `READ_ONLY=false`（默认） | 具备所需写权限的受限身份 |
| staging 受限写入 | `READ_ONLY=false` + `NAMESPACE_ALLOWLIST=staging` | 仅 `staging` 的 RoleBinding |
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

## in-cluster 配置示例

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: k8s-mcp
  namespace: ops
spec:
  serviceAccountName: k8s-mcp-reader
  containers:
    - name: server
      image: your-registry/k8s-mcp:tag
      command: ["k8s-mcp", "serve"]
      env:
        - name: K8S_MCP_READ_ONLY
          value: "false"
        - name: K8S_MCP_MAX_CONCURRENT_TOOLS
          value: "4"
```

远程 MCP transport 还需网络层认证、TLS、网络策略、审计和请求大小限制；本仓库默认 transport 是 stdio。

## 上线前检查

- [ ] 只读实例显式设置了 `READ_ONLY=true`。
- [ ] 写实例有精确 namespace allowlist。
- [ ] 写入 RBAC 不含不必要的 secrets、RBAC 管理或 cluster-scoped 权限。
- [ ] webhook 使用 HTTPS 与精确 host allowlist。
- [ ] 审计日志可追踪该 ServiceAccount。
