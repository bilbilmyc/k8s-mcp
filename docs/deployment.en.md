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

## Where the server runs and how it connects

k8s-mcp currently supports **stdio transport only**: the MCP client (Claude Desktop, Cursor, …) launches `k8s-mcp serve` as a **local subprocess**, the server runs on the same machine as the client, and reaches the Kubernetes API over the network with its credentials. There is deliberately no "run the server as an in-cluster Pod and point a remote MCP client at it" topology — stdio requires the subprocess to live on the client machine. A remote HTTP transport is on the [roadmap](./ROADMAP.md) (v2+); landing it also requires transport-layer authentication, TLS, network policies, auditing, and request-size limits.

Two supported combinations, depending on where the server runs and which identity it uses:

### Option 1: local kubeconfig (default for personal dev/ops)

The server uses the same kubeconfig as your `kubectl`; its permissions are your personal RBAC identity:

```bash
export KUBECONFIG="$HOME/.kube/config"
export K8S_MCP_KUBE_CONTEXT=my-cluster   # optional, for multi-context setups
k8s-mcp serve
```

### Option 2: local server + ServiceAccount token against the apiserver (shared/restricted identity)

Create a restricted ServiceAccount from the `deploy/rbac/` templates, mint a token, and let the local server run with that **least-privilege cluster identity** — useful when the identity should be a dedicated account rather than your personal kubeconfig:

```bash
# 1. Create the restricted ServiceAccount (templates above)
kubectl apply -f deploy/rbac/read-only.yaml

# 2. Mint a time-bound token and hand it to the server (auth mode A)
export K8S_MCP_API_SERVER="https://api.example.com:6443"
export K8S_MCP_API_TOKEN="$(kubectl -n ops create token k8s-mcp-reader --duration=8h)"
export K8S_MCP_API_CA_CERT="$HOME/.kube/ca.crt"   # required with a private CA
k8s-mcp serve
```

Tokens expire; re-mint when they do. Never commit them to client configs or the repository.

### Option 3: in-cluster sidecar (subprocess launched inside the Agent container)

The stdio server must be launched as a child process by an MCP client in the same container. Bundle `k8s-mcp` into the Agent image and use this Pod fragment to run the full Agent + MCP combination in-cluster — it is a client-side subprocess, not a standalone remote service:

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
