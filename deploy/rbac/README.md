# RBAC templates

Use `read-only.yaml` for a diagnostic baseline. `namespace-operator.yaml` is intentionally a template: replace `<namespace>` and remove permissions before applying. See `docs/deployment.md` / `docs/deployment.en.md`.

## NVIDIA GPU diagnostics

`nvidia-gpu-read-only.yaml` grants only the read permissions required by the five `gpu_*` diagnostic tools: Nodes, Pods, Deployment/Job templates, and the optional NVIDIA GPU Operator `ClusterPolicy` CR. Replace the example ServiceAccount namespace (`default`) before applying it. The template grants no mutation, no Pod exec, and no Secret access.
