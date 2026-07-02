"""Node operations: cordon / uncordon / drain.

These mirror `kubectl cordon | kubectl uncordon | kubectl drain` and share the
underlying primitives:

  - `cordon` patches spec.unschedulable=true on a Node.
  - `uncordon` patches spec.unschedulable=false.
  - `drain` cordons the node, then evicts (or deletes) all pods that aren't
    part of a DaemonSet and don't have emptyDir volumes — those require
    --ignore-daemonsets / --delete-emptydir-data to evict.

中文说明：
节点运维操作（重启 / 升级 / 维护）三件套：

  - `cordon_node`：标记 unschedulable=true，新 Pod 不会再调度上来。
  - `uncordon_node`：恢复调度。
  - `drain_node`：先 cordon，再用 Eviction API 驱逐 Pod（尊重 PDB）。
    DaemonSet / emptyDir Pod 默认跳过；`force=True` 绕过 PDB 用 raw delete。

执行排障 reboot / 内核升级 / kubelet 重启等场景的标准动作：
cordon → drain → 维护 → uncordon。

Drain is high-risk: it removes workloads from a node. We respect
PodDisruptionBudgets via the Eviction API by default (same as `kubectl drain`
without --force). Set --force to bypass PDBs (uses raw delete, which is
disruptive but unblocks stuck drains).
"""
from __future__ import annotations

import logging
import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from ..client import get_api_client
from ..config import get_settings

logger = logging.getLogger(__name__)


def _core_v1():
    return client.CoreV1Api(get_api_client())


def _read_only_guard():
    if get_settings().read_only:
        raise PermissionError("Server is in read-only mode.")


def cordon_node(name: str) -> str:
    """Mark a Node as unschedulable (no new pods will land on it).

    Args:
        name: node name.

    Equivalent to `kubectl cordon <name>`.
    """
    _read_only_guard()
    core = _core_v1()
    body = {"spec": {"unschedulable": True}}
    try:
        core.patch_node(name, body)
    except ApiException as e:
        if e.status == 404:
            raise LookupError(f"Node '{name}' not found") from e
        raise
    return f"Node/{name} cordoned (unschedulable=true)"


def uncordon_node(name: str) -> str:
    """Mark a Node as schedulable again (reverse of cordon).

    Equivalent to `kubectl uncordon <name>`.
    """
    _read_only_guard()
    core = _core_v1()
    body = {"spec": {"unschedulable": False}}
    try:
        core.patch_node(name, body)
    except ApiException as e:
        if e.status == 404:
            raise LookupError(f"Node '{name}' not found") from e
        raise
    return f"Node/{name} uncordoned (unschedulable=false)"


def drain_node(
    name: str,
    ignore_daemonsets: bool = False,
    delete_emptydir_data: bool = False,
    force: bool = False,
    grace_period_seconds: int = -1,
    timeout_seconds: int = 60,
) -> str:
    """Drain a Node: cordon + evict all evictable pods.

    Args:
        name: node name.
        ignore_daemonsets: evict DaemonSet pods (default False; matches kubectl).
        delete_emptydir_data: evict pods that use emptyDir (default False).
        force: bypass PodDisruptionBudgets and force-delete stuck pods.
        grace_period_seconds: grace period for the eviction (-1 = pod default).
            When --force, a non-positive value means immediate kill.
        timeout_seconds: total drain timeout (default 60s).

    Mirrors `kubectl drain`. Returns a summary of what happened.

    Safety notes:
      - Without --ignore-daemonsets / --delete-emptydir-data we refuse to evict
        those pods and report them, so you can decide whether to retry with
        flags set.
      - With --force we DELETE rather than evict, so PDBs are bypassed.
    """
    _read_only_guard()
    core = _core_v1()

    try:
        core.read_node(name)
    except ApiException as e:
        if e.status == 404:
            raise LookupError(f"Node '{name}' not found") from e
        raise

    # Step 1: cordon
    core.patch_node(name, {"spec": {"unschedulable": True}})

    # Step 2: list pods on the node, across all namespaces
    pods = core.list_pod_for_all_namespaces(field_selector=f"spec.nodeName={name}")
    deadline = time.monotonic() + timeout_seconds

    evicted: list[str] = []
    skipped_daemonset: list[str] = []
    skipped_emptydir: list[str] = []
    errors: list[str] = []

    for pod in pods.items:
        ns = pod.metadata.namespace
        pod_name = pod.metadata.name
        owner_kinds = _owner_kinds(pod)
        pods_api = _core_v1()

        # DaemonSet pods aren't evictable by default
        if "DaemonSet" in owner_kinds and not ignore_daemonsets:
            skipped_daemonset.append(f"{ns}/{pod_name}")
            continue

        # EmptyDir pods can't be safely evicted unless the user opts in
        if not delete_emptydir_data and _has_emptydir(pod):
            skipped_emptydir.append(f"{ns}/{pod_name}")
            continue

        if time.monotonic() > deadline:
            errors.append(f"{ns}/{pod_name}: drain timeout")
            continue

        try:
            if force:
                # Raw delete bypasses PDB; honor grace_period for shutdown.
                delete_options = client.V1DeleteOptions()
                if grace_period_seconds >= 0:
                    delete_options.grace_period_seconds = grace_period_seconds
                pods_api.delete_namespaced_pod(pod_name, ns, body=delete_options)
                evicted.append(f"{ns}/{pod_name} (forced delete)")
            else:
                # Eviction API: respects PDBs.
                ev_body = client.V1Eviction(
                    metadata=client.V1ObjectMeta(name=pod_name, namespace=ns),
                    delete_options=client.V1DeleteOptions(
                        grace_period_seconds=grace_period_seconds
                    ),
                )
                core.create_namespaced_pod_eviction(pod_name, ns, body=ev_body)
                evicted.append(f"{ns}/{pod_name} (evicted)")
        except ApiException as e:
            errors.append(f"{ns}/{pod_name}: {e.reason or e.status}")

    lines = [f"Node/{name} drained."]
    lines.append(f"Evicted ({len(evicted)}): " + (", ".join(evicted) if evicted else "none"))
    if skipped_daemonset:
        lines.append(
            f"Skipped DaemonSet pods ({len(skipped_daemonset)}): "
            + ", ".join(skipped_daemonset)
            + " — pass ignore_daemonsets=True to evict them"
        )
    if skipped_emptydir:
        lines.append(
            f"Skipped emptyDir pods ({len(skipped_emptydir)}): "
            + ", ".join(skipped_emptydir)
            + " — pass delete_emptydir_data=True to evict them"
        )
    if errors:
        lines.append(f"Errors ({len(errors)}): " + ", ".join(errors))
    return "\n".join(lines)


# ---------- internals ----------------------------------------------------------


def _owner_kinds(pod) -> list[str]:
    """Return the `kind` of each owner reference (e.g. ['DaemonSet'])."""
    refs = (pod.metadata.owner_references or [])
    return [r.kind for r in refs if r.kind]


def _has_emptydir(pod) -> bool:
    """True if any container or init container / volume uses an emptyDir."""
    vols = (pod.spec.volumes or [])
    if any(v.empty_dir is not None for v in vols):
        return True
    return False


def register(mcp) -> None:
    mcp.tool()(cordon_node)
    mcp.tool()(uncordon_node)
    mcp.tool()(drain_node)
