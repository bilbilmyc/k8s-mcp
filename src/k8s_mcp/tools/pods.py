"""Pod listing + exec.

中文说明：
- `list_pods`：支持 namespace / label_selector / field_selector 三类筛选，
  `include_all=True` 跨所有 namespace。
- `exec_pod`：⚠️ 高权限 — 在 Pod 容器里跑命令（批模式）。K8s RBAC 控制
  谁能 pods/exec，本工具不做命令白名单。
  Pod 删除请走通用两段式 `delete_resource(kind="Pod", ...)`。
"""
from __future__ import annotations

from kubernetes import client
from kubernetes.client.rest import ApiException
from kubernetes.stream.ws_client import (
    STDERR_CHANNEL,
    STDOUT_CHANNEL,
)

from ..client import get_api_client
from ..config import get_settings
from ..formatters import format_age, short_table

MAX_EXEC_TIMEOUT_SECONDS = 600


def _core_v1():
    return client.CoreV1Api(get_api_client())


def list_pods(
    namespace: str | None = None,
    label_selector: str | None = None,
    field_selector: str | None = None,
    include_all: bool = False,
) -> str:
    """List Pods with Pod-specific columns (PHASE / RESTARTS / NODE). For a
    generic cross-kind list, prefer `list_resources(kind="Pod", ...)` — that
    one works on any kind (including CRDs); use THIS tool only when you need
    Pod-specific columns or the `include_all` Succeeded/Failed filter.
    Equivalent to `kubectl get pods`.

    Note: prefer reusing the most recent result for the same query rather
    than re-calling if the underlying state is unlikely to have changed. New
    calls remain valid when verifying a mutation's effect.

    Args:
        namespace: namespace to list; None = all namespaces.
        label_selector: e.g. "app=nginx".
        field_selector: e.g. "status.phase=Running" or "spec.nodeName=node-1".

    Returns a NAME / NAMESPACE / PHASE / RESTARTS / AGE / NODE table.
    """
    api = _core_v1()
    if namespace:
        ret = api.list_namespaced_pod(
            namespace,
            label_selector=label_selector,
            field_selector=field_selector,
        )
    else:
        ret = api.list_pod_for_all_namespaces(
            label_selector=label_selector,
            field_selector=field_selector,
        )

    rows = []
    for pod in ret.items:
        phase = (pod.status.phase or "")
        if not include_all and phase in ("Succeeded", "Failed", "Evicted"):
            continue
        restarts = sum(cs.restart_count for cs in (pod.status.container_statuses or []))
        rows.append({
            "NAME": pod.metadata.name,
            "NAMESPACE": pod.metadata.namespace,
            "PHASE": phase,
            "RESTARTS": str(restarts),
            "AGE": format_age(pod.metadata.creation_timestamp),
            "NODE": pod.spec.node_name or "",
        })

    return short_table(rows, ["NAME", "NAMESPACE", "PHASE", "RESTARTS", "AGE", "NODE"])


def exec_pod(
    pod_name: str,
    command: list[str],
    namespace: str | None = "default",
    container: str | None = None,
    timeout_seconds: int = 30,
) -> str:
    """⚠️ HIGH-PRIVILEGE — Run a command in a pod and return the output.

    Batch mode: runs the command to completion, captures stdout and
    stderr separately, returns the combined output with the exit code.
    NOT a TTY / interactive shell — pipes only. Use for diagnostics
    like `ls`, `cat /etc/config`, `env`, `ps`, `curl <url>` (one-shot).

    Args:
        pod_name: pod to exec into.
        command: command as **argv list**, e.g. `["ls", "-la"]`. NOT
            executed via shell — argv is passed directly to the
            container's exec, avoiding shell injection. For shell
            features (pipes, redirects), wrap explicitly:
            `["sh", "-c", "ps aux | grep nginx"]`.
        namespace: pod namespace. Default `"default"`.
        container: container name. Defaults to the first container if
            the pod has only one. If the pod has multiple containers
            and `container=` is omitted, raises with the list of
            available containers — pick one explicitly to avoid
            kubectl-style surprises.
        timeout_seconds: hard wall-clock cap on the call (1–600,
            default 30). On timeout the WebSocket is closed and the
            function returns a timeout message; the command inside the
            pod is NOT guaranteed to be killed (K8s exec protocol has
            no cancel — kubelet usually terminates it on WS close,
            but don't bet on it for long-running `sleep` / `tail -f`).

    Returns:
        Formatted output::

            $ <command>
            <stdout>
            <stderr>
            (exit code: <N>)

        stdout / stderr are omitted when empty. Exit code is `0` for
        success, non-zero otherwise. On timeout the format is::

            $ <command>
            ❌ exec timeout after <N>s (pod may still be running the command)

    Raises:
        PermissionError: read-only mode blocks the call, or the target
            namespace is not in `K8S_MCP_NAMESPACE_ALLOWLIST`.
        ValueError: empty pod name / command, pod not found, container
            not found, or ambiguous container in multi-container pod.
        RuntimeError: WebSocket / exec protocol failure, RBAC denied.
    """
    if not pod_name or not isinstance(pod_name, str):
        raise ValueError("pod_name must be a non-empty string")
    if (
        not command
        or not isinstance(command, list)
        or not all(isinstance(c, str) and c for c in command)
    ):
        raise ValueError("command must be a non-empty list of non-empty strings")
    if (
        not isinstance(timeout_seconds, int)
        or timeout_seconds < 1
        or timeout_seconds > MAX_EXEC_TIMEOUT_SECONDS
    ):
        raise ValueError(
            f"timeout_seconds must be an int between 1 and {MAX_EXEC_TIMEOUT_SECONDS}"
        )

    settings = get_settings()
    if settings.read_only:
        raise PermissionError(
            "Server is in read-only mode (K8S_MCP_READ_ONLY=true). exec is disabled."
        )
    if not settings.ns_allowed(namespace):
        raise PermissionError(
            f"Exec in namespace '{namespace}' is not allowed by "
            f"K8S_MCP_NAMESPACE_ALLOWLIST"
        )

    core = _core_v1()

    # Validate pod + auto-pick container.
    try:
        pod = core.read_namespaced_pod(name=pod_name, namespace=namespace)
    except ApiException as e:
        if e.status == 404:
            raise ValueError(f"pod {namespace}/{pod_name} not found") from e
        raise RuntimeError(
            f"failed to read pod {namespace}/{pod_name}: {e.status} {e.reason}"
        ) from e

    containers = [c.name for c in (pod.spec.containers or [])]
    if not containers:
        raise ValueError(f"pod {namespace}/{pod_name} has no containers")

    if container is None:
        if len(containers) == 1:
            container = containers[0]
        else:
            raise ValueError(
                f"pod {namespace}/{pod_name} has multiple containers — "
                f"specify `container=` (one of: {containers})"
            )
    elif container not in containers:
        raise ValueError(
            f"container {container!r} not in pod {namespace}/{pod_name} "
            f"(available: {containers})"
        )

    # Open the exec WebSocket. _preload_content=False returns a WSClient
    # instead of auto-blocking on read; we drive run_forever ourselves
    # so we can apply a wall-clock timeout.
    try:
        ws = core.connect_get_namespaced_pod_exec(
            name=pod_name,
            namespace=namespace,
            command=command,
            container=container,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
            _preload_content=False,
        )
    except ApiException as e:
        raise RuntimeError(
            f"failed to start exec in {namespace}/{pod_name}/{container}: "
            f"{e.status} {e.reason}"
        ) from e

    cmd_str = " ".join(command)
    try:
        ws.run_forever(timeout=timeout_seconds)
    except Exception as e:
        try:
            ws.close()
        except Exception:
            pass
        raise RuntimeError(f"exec WebSocket error: {e}") from e

    if ws.is_open():
        # run_forever returned but the WS is still open — wall-clock
        # timeout fired. Close and surface a clear timeout message.
        try:
            ws.close()
        except Exception:
            pass
        return (
            f"$ {cmd_str}\n"
            f"❌ exec timeout after {timeout_seconds}s "
            f"(pod may still be running the command)"
        )

    stdout = ws.read_channel(STDOUT_CHANNEL)
    stderr = ws.read_channel(STDERR_CHANNEL)
    # `returncode` reads ERROR_CHANNEL on first access; per K8s exec
    # protocol, status="Success" → 0, otherwise the cause message is
    # the exit code.
    exit_code = ws.returncode

    lines = [f"$ {cmd_str}"]
    if stdout:
        lines.append(stdout.rstrip("\n"))
    if stderr:
        lines.append(stderr.rstrip("\n"))
    lines.append(f"(exit code: {exit_code if exit_code is not None else 'unknown'})")
    return "\n".join(lines)


def register(mcp) -> None:
    mcp.tool()(list_pods)
    mcp.tool()(exec_pod)
