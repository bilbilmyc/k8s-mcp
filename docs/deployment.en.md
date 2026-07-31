# Deployment and RBAC

[中文](./deployment.md) · [Documentation](./README.en.md)

## Recommended topology

| Environment | MCP configuration | Kubernetes identity |
| --- | --- | --- |
| Development / normal operations | Default policy (no `READ_ONLY=false` override) | Restricted identity with the required write access |
| Scoped staging writes | `NAMESPACE_ALLOWLIST=staging` | RoleBinding only in `staging` |
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

## In-cluster stdio client fragment

The stdio server must be launched as a child process by an MCP client in the same container. Merge this authentication fragment into the Agent or MCP client Pod; it is not a standalone remote service:

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

Configure that client to launch `command: k8s-mcp` inside the container. A standalone deployment first needs a remote MCP transport plus authentication, TLS, network policies, auditing, and request-size limits; this repository currently provides stdio only.

## Go-live checklist

- [ ] Read-only instances explicitly set `READ_ONLY=true`.
- [ ] Write instances use an exact namespace allowlist.
- [ ] Write RBAC does not include unnecessary secret, RBAC-management, or cluster-scoped permissions.
- [ ] Webhooks use HTTPS and an exact host allowlist.
- [ ] Audit logs can identify the ServiceAccount.
