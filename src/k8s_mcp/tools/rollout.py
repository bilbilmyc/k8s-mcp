"""Rollout status and rollback (Deployment / StatefulSet).

中文说明：
- `rollout_status`：轮询直到 Ready / Available / 失败 / 超时
- `rollout_history`：列出 ControllerRevision 记录，附带 image 字段
  方便 Agent 选 revision
- `rollout_undo`：回滚到指定 revision（默认上一次）

仅支持 Deployment / StatefulSet（DaemonSet 没有 ControllerRevision）。
"""
from __future__ import annotations

import logging
import time

from kubernetes.client.rest import ApiException

from ..client import get_api_client
from ..config import get_settings
from ..formatters import short_table

logger = logging.getLogger(__name__)


def _apps_v1():
    from kubernetes import client
    return client.AppsV1Api(get_api_client())


def _read_only_guard():
    if get_settings().read_only:
        raise PermissionError("Server is in read-only mode.")


def _ensure_ns(namespace: str):
    if not get_settings().ns_allowed(namespace):
        raise PermissionError(
            f"Write to namespace '{namespace}' is not allowed"
        )


def rollout_status(
    kind: str, name: str, namespace: str = "default",
    timeout_seconds: int = 60, watch: bool = False,
) -> str:
    """Wait for a Deployment/StatefulSet rollout to finish.

    Args:
        kind: "Deployment" or "StatefulSet".
        name, namespace: workload identity.
        timeout_seconds: max wait time (default 60s).
        watch: if True, poll every 2 seconds until the rollout completes or the
            timeout is hit. If False, return the current state immediately.

    Returns a human-readable status (and progress if watch=True).
    """
    kind_lower = kind.lower()
    if kind_lower not in ("deployment", "statefulset"):
        raise ValueError(f"Unsupported kind for rollout_status: {kind}")

    api = _apps_v1()
    deadline = time.monotonic() + timeout_seconds
    last_msg = ""
    while True:
        msg = _status_once(api, kind_lower, name, namespace)
        if msg != last_msg:
            logger.info("rollout %s/%s: %s", kind_lower, name, msg)
            last_msg = msg
        if not watch:
            return msg
        if _is_done(msg):
            return msg
        if time.monotonic() > deadline:
            return f"{msg}\n[timeout after {timeout_seconds}s; rollout may still be in progress]"
        time.sleep(2)


def _status_once(api, kind_lower: str, name: str, namespace: str) -> str:
    try:
        if kind_lower == "deployment":
            obj = api.read_namespaced_deployment(name, namespace)
        else:
            obj = api.read_namespaced_stateful_set(name, namespace)
    except ApiException as e:
        if e.status == 404:
            return f"Error: {kind_lower.capitalize()} '{namespace}/{name}' not found"
        raise

    spec_gen = (obj.metadata.generation or 0)
    status_gen = (obj.status.observed_generation or 0)
    progressing = next((c for c in (obj.status.conditions or []) if c.type == "Progressing"), None)

    if spec_gen != status_gen:
        return f"Waiting for rollout to start: spec gen {spec_gen} != observed {status_gen}"

    if not progressing:
        return f"No Progressing condition found (current observed gen {status_gen})"

    if progressing.status == "True" and progressing.reason == "NewReplicaSetAvailable":
        return f"Rollout complete: {progressing.message or 'ok'}"

    if progressing.status == "False":
        return f"Rollout stalled: {progressing.reason} — {progressing.message}"

    return f"Rollout in progress: {progressing.reason or '...'} — {progressing.message or ''}"


def _is_done(msg: str) -> bool:
    return msg.startswith("Rollout complete") or msg.startswith("Error:") or msg.startswith("Rollout stalled")


def rollout_undo(
    kind: str, name: str, namespace: str = "default",
    to_revision: int | None = None,
) -> str:
    """⚠️ WRITE / ⚠️ SILENT ROLLBACK — by default rolls back to the previous
    revision (no prompt). Pass `to_revision=N` to target a specific revision;
    use `rollout_history` first to see what's available.

    Args:
        kind: "Deployment" or "StatefulSet".
        name, namespace: workload identity.
        to_revision: specific revision to roll back to (default: previous).

    This calls the server's rollback endpoint (Deployment) or patches the
    StatefulSet's pod template back to the previous revision.
    """
    _read_only_guard()
    _ensure_ns(namespace)
    kind_lower = kind.lower()
    if kind_lower not in ("deployment", "statefulset"):
        raise ValueError(f"Unsupported kind for rollout_undo: {kind}")

    api = _apps_v1()

    if kind_lower == "deployment":
        body = {"kind": "DeploymentRollback", "apiVersion": "apps/v1", "name": name}
        if to_revision is not None:
            body["revision"] = int(to_revision)
        try:
            api.create_namespaced_deployment_rollback(namespace, body)
        except ApiException as e:
            if e.status == 404:
                raise LookupError(f"Deployment '{namespace}/{name}' not found") from e
            raise
        target = f" revision {to_revision}" if to_revision is not None else "previous revision"
        return f"Deployment/{namespace}/{name} rolled back to{target}"

    # StatefulSet has no built-in rollback endpoint; we manually patch the
    # pod template back to a previous ControllerRevision.
    try:
        ss = api.read_namespaced_stateful_set(name, namespace)
    except ApiException as e:
        if e.status == 404:
            raise LookupError(f"StatefulSet '{namespace}/{name}' not found") from e
        raise

    revs = api.list_namespaced_controller_revision(namespace, label_selector=(
        f"app.kubernetes.io/name={name}" if False else None
    ))
    # Find the current revision; pick the one before it
    cur_rev = (ss.status.current_revision or "").split(",")[0].strip()
    rev_list = sorted(revs.items, key=lambda r: r.revision, reverse=True)
    target_rev = None
    for r in rev_list:
        if r.revision < int(cur_rev or 0) and to_revision in (None, r.revision):
            target_rev = r
            break
    if target_rev is None:
        raise LookupError(
            f"No previous revision found for StatefulSet '{namespace}/{name}'"
        )

    # Read the previous template from the ControllerRevision
    template = target_rev.data.get("spec", {}).get("template")
    if not template:
        raise RuntimeError(f"ControllerRevision {target_rev.metadata.name} has no spec.template")

    api.patch_namespaced_stateful_set(name, namespace, {"spec": {"template": template}})
    return f"StatefulSet/{namespace}/{name} rolled back to revision {target_rev.revision}"


def rollout_history(kind: str, name: str, namespace: str = "default") -> str:
    """Show the rollout history of a Deployment/StatefulSet.

    Args:
        kind: "Deployment" or "StatefulSet".
        name, namespace: workload identity.

    Lists the available `ControllerRevision` revisions — use the revision
    numbers with `rollout_undo(to_revision=...)`.
    """
    kind_lower = kind.lower()
    if kind_lower not in ("deployment", "statefulset"):
        raise ValueError(f"Unsupported kind for rollout_history: {kind}")

    api = _apps_v1()
    # ControllerRevisions are labeled with the workload name (legacy
    # deployments use `pod-template-hash`, but the workload name is the
    # most stable filter).
    try:
        revs = api.list_namespaced_controller_revision(
            namespace, label_selector=f"app.kubernetes.io/name={name}",
        )
        # Fallback: list all and filter manually if the label isn't there
        if not revs.items:
            revs = api.list_namespaced_controller_revision(namespace)
            revs.items = [r for r in revs.items if _belongs_to(r, name, namespace)]
    except ApiException as e:
        if e.status == 404:
            raise LookupError(f"{kind} '{namespace}/{name}' not found") from e
        raise

    if not revs.items:
        return f"(no rollout history for {kind}/{namespace}/{name})"

    rows = []
    for r in sorted(revs.items, key=lambda x: x.revision, reverse=True):
        rows.append({
            "REVISION": str(r.revision),
            "CREATED": str(r.metadata.creation_timestamp or ""),
            "NAME": r.metadata.name,
            "IMAGES": _extract_images(r.data.get("spec", {}).get("template")),
        })
    return short_table(rows, ["REVISION", "CREATED", "NAME", "IMAGES"])


def _belongs_to(rev, workload_name: str, namespace: str) -> bool:
    """True if the ControllerRevision's owner references our workload."""
    refs = (rev.metadata.owner_references or [])
    return any(
        r.kind in ("Deployment", "StatefulSet") and r.name == workload_name
        for r in refs
    )


def _extract_images(template) -> str:
    """Pull container image names from a PodTemplate spec."""
    if not template:
        return ""
    containers = (template.get("spec") or {}).get("containers") or []
    return ",".join(c.get("image", "") for c in containers)


def register(mcp) -> None:
    mcp.tool()(rollout_status)
    mcp.tool()(rollout_undo)
    mcp.tool()(rollout_history)
