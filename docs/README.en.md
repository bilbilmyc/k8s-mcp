# Documentation

[中文](./README.md) · [Project home](../README.en.md)

The documentation follows a deliberate order: secure the server, connect it, then grant only the authorization needed. The project exposes **90 tools**; CI keeps this inventory, the home pages, and release checks aligned.

## New-user path

1. [Quick start](./quickstart.en.md): installation, authentication, client setup, and `doctor`.
2. [Security](./security.en.md): on-demand read-only mode, write boundaries, concurrency limits, and webhook boundaries.
3. [Deployment](./deployment.en.md): from a read-only identity to namespace-scoped writes.
4. [Tool catalog](./tools-reference.md): complete signatures.

## Documentation map

| Topic | 中文 | English |
| --- | --- | --- |
| Getting started, auth, MCP clients | [quickstart.md](./quickstart.md) | [quickstart.en.md](./quickstart.en.md) |
| Security, migration, runtime gates | [security.md](./security.md) | [security.en.md](./security.en.md) |
| ServiceAccount and least-privilege RBAC | [deployment.md](./deployment.md) | [deployment.en.md](./deployment.en.md) |
| Every `K8S_MCP_*` variable | [env.md](./env.md) | [env.en.md](./env.en.md) |
| NVIDIA GPU / AI workloads | [gpu.md](./gpu.md) | [gpu.en.md](./gpu.en.md) |
| Full **90 tools** signature catalog | [tools-reference.md](./tools-reference.md) | [tools-reference.md](./tools-reference.md) |
| Deep dives and workflows | [tools.md](./tools.md) | — |
| Direct Python calls | [usage.md](./usage.md) | — |
| Examples and troubleshooting | [examples.md](./examples.md) / [troubleshooting.md](./troubleshooting.md) | — |
| Architecture, publishing, changes | [architecture.md](./architecture.md) / [publishing.md](./publishing.md) | — |

> [!NOTE]
> [PLAN.md](./PLAN.md) is archived design material. It can describe removed behavior such as two-phase deletion; follow the current README, security documentation, and tool docstrings instead.
