# Deployment and RBAC

[中文](./deployment.md) · [Documentation](./README.en.md)

## Recommended topology

| Environment | MCP configuration | Kubernetes identity |
| --- | --- | --- |
| Development / normal operations | `READ_ONLY=false` (default) | Restricted identity with the required write access |
| Scoped staging writes | `READ_ONLY=false` + `NAMESPACE_ALLOWLIST=staging` | RoleBinding only in `staging` |
| Audit / diagnostics | `READ_ONLY=true` | Read-only ServiceAccount or personal read-only kubeconfig |

## Quick deployment templates

```bash
kubectl apply -f deploy/rbac/read-only.yaml
# Replace <namespace> in namespace-operator.yaml before applying it.
kubectl apply -f deploy/rbac/namespace-operator.yaml
```

- [read-only.yaml](../deploy/rbac/read-only.yaml) creates a ServiceAccount bound to Kubernetes’ built-in `view` ClusterRole.
- [namespace-operator.yaml](../deploy/rbac/namespace-operator.yaml) is an example namespace write policy. Remove unused resources and verbs, then replace `<namespace>`.

> [!CAUTION]
> These templates are a starting point, not universal production permissions. Never bind `cluster-admin` to an MCP ServiceAccount, and do not expand to `*` simply to clear one `Forbidden` error.

## In-cluster configuration example

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

For a remote MCP transport, add authentication, TLS, network policies, auditing, and request-size limits at the transport layer. This repository’s default transport is stdio.

## Go-live checklist

- [ ] Read-only instances explicitly set `READ_ONLY=true`.
- [ ] Write instances use an exact namespace allowlist.
- [ ] Write RBAC does not include unnecessary secret, RBAC-management, or cluster-scoped permissions.
- [ ] Webhooks use HTTPS and an exact host allowlist.
- [ ] Audit logs can identify the ServiceAccount.
