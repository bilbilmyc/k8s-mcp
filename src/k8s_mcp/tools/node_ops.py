"""Node operations: list_nodes, label_node / unlabel_node, taint_node /
untaint_node, cordon / uncordon / drain.

These mirror their `kubectl` counterparts and share the underlying primitives:

  - `list_nodes` — node-specific columns (ROLE / STATUS / AGE / INTERNAL_IP /
    TAINT_SUMMARY); richer than `list_resources(kind="Node")`.
  - `cordon_node` patches spec.unschedulable=true on a Node.
  - `uncordon_node` patches spec.unschedulable=false.
  - `taint_node` / `untaint_node` add or remove a single taint
    (key=value:effect or key:effect); atomic JSON Patch.
  - `label_node` / `unlabel_node` add or remove a single label; RFC 6901
    token escaping for keys containing `/` or `~`.
  - `drain_node` cordons the node, then evicts (or deletes) all pods that aren't
    part of a DaemonSet and don't have emptyDir volumes — those require
    --ignore-daemonsets / --delete-emptydir-data to evict.

中文说明：
节点运维操作（重启 / 升级 / 维护 / 调度）：
  - `list_nodes`：节点专属列视图（ROLE / STATUS / TAINT 等）。
  - `cordon_node` / `uncordon_node`：调度开关。
  - `taint_node` / `untaint_node`：单条污点增删（专用节点池 / GPU 隔离）。
  - `label_node` / `unlabel_node`：单条 label 增删（helm / operator 前置条件）。
  - `drain_node`：先 cordon，再用 Eviction API 驱逐 Pod（尊重 PDB）。

执行排障 reboot / 内核升级 / kubelet 重启等场景的标准动作：
cordon → drain → 维护 → uncordon。

Drain is high-risk: it removes workloads from a node. We respect
PodDisruptionBudgets via the Eviction API by default (same as `kubectl drain`
without --force). Set --force to bypass PDBs (uses raw delete, which is
disruptive but unblocks stuck drains).
"""
from __future__ import annotations

import logging
import re
import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from ..client import get_api_client
from ..config import get_settings
from ..formatters import format_age, short_table

logger = logging.getLogger(__name__)


def _core_v1():
    return client.CoreV1Api(get_api_client())


def _read_only_guard(action: str) -> None:
    if get_settings().read_only:
        raise PermissionError(
            f"Server is in read-only mode (K8S_MCP_READ_ONLY=true). "
            f"{action} is disabled."
        )


def cordon_node(name: str) -> str:
    """⚠️ WRITE — patches Node `spec.unschedulable=true`; new Pods will NOT
    be scheduled onto this node. Existing Pods are NOT evicted.

    Equivalent to `kubectl cordon <name>`. To also evict existing workloads,
    use `drain_node` instead. To reverse, use `uncordon_node`.

    Args:
        name: node name.
    """
    _read_only_guard("cordon_node")
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
    _read_only_guard("uncordon_node")
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
    """⚠️ WRITE / ⚠️ DISRUPTIVE — cordon + evict all evictable Pods from a
    Node. Removes running workloads; PDBs are bypassed when `force=True`.

    Args:
        name: node name.
        ignore_daemonsets: evict DaemonSet pods (default False; matches kubectl).
        delete_emptydir_data: evict pods that use emptyDir (default False).
        force: bypass PodDisruptionBudgets and force-delete stuck pods.
        grace_period_seconds: grace period for the eviction (-1 = pod default).
            When --force, a non-positive value means immediate kill.
        timeout_seconds: total drain timeout (default 60s).

    Mirrors `kubectl drain`. Returns a summary of what happened.

    Safety:
      - Without --ignore-daemonsets / --delete-emptydir-data we refuse to evict
        those pods and report them, so you can decide whether to retry with
        flags set.
      - With --force we DELETE rather than evict, so PDBs are bypassed.
    """
    _read_only_guard("drain_node")
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


# ---------- listing & label/taint ops -----------------------------------------


_NODE_NAME_RE = re.compile(
    r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?(\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)*$"
)

# RFC 1123 label value — empty OK, alphanumeric + dash/underscore/dot.
_LABEL_KEY_RE = re.compile(
    r"^([a-zA-Z0-9]([-a-zA-Z0-9_.]{0,61}[a-zA-Z0-9])?/?)*$"
)
_LABEL_VAL_RE = re.compile(r"^([a-zA-Z0-9]([-a-zA-Z0-9_.]{0,61}[a-zA-Z0-9])?)?$")

# Taint effects as defined by K8s. Unknown effects are refused so the
# apiserver's own validator isn't relied on for client-side gating.
_VALID_TAINT_EFFECTS = ("NoSchedule", "PreferNoSchedule", "NoExecute")


def _validate_node_name(name: str) -> None:
    if not name or not _NODE_NAME_RE.match(name):
        raise ValueError(f"invalid node name: {name!r} (RFC 1123)")


def _validate_label_key(key: str) -> None:
    if not key or not _LABEL_KEY_RE.match(key):
        raise ValueError(f"invalid label key: {key!r}")


def _validate_label_value(value: str) -> None:
    if value is None:
        return
    if not _LABEL_VAL_RE.match(value):
        raise ValueError(f"invalid label value: {value!r}")


def _parse_taint_spec(spec: str) -> tuple[str, str, str]:
    """Parse a taint spec of the form `key=value:effect` or `key:effect`.

    Returns (key, value, effect). `value` defaults to '' when omitted.
    Raises ValueError on malformed input or unknown effect.
    """
    if not spec or ":" not in spec:
        raise ValueError(
            f"invalid taint spec: {spec!r}; expected 'key=value:effect' "
            f"or 'key:effect'"
        )
    key_part, _, effect = spec.rpartition(":")
    effect = effect.strip()
    if effect not in _VALID_TAINT_EFFECTS:
        raise ValueError(
            f"invalid taint effect: {effect!r}; must be one of {_VALID_TAINT_EFFECTS}"
        )
    if "=" in key_part:
        key, _, value = key_part.partition("=")
        key, value = key.strip(), value.strip()
    else:
        key, value = key_part.strip(), ""
    if not key or not _NODE_NAME_RE.match(key):
        raise ValueError(f"invalid taint key: {key!r}")
    return key, value, effect


def _jsonpatch_escape(token: str) -> str:
    """RFC 6901 §4 token escaping for JSON Patch paths."""
    return token.replace("~", "~0").replace("/", "~1")


def _node_status_summary(node) -> tuple[str, str]:
    """Return (status, roles) for a Node — concise columns for `list_nodes`."""
    conditions = node.status.conditions or []
    ready = next((c for c in conditions if c.type == "Ready"), None)
    if ready is None:
        status = "Unknown"
    elif str(ready.status) == "True":
        status = "Ready"
    else:
        status = "NotReady"

    labels = node.metadata.labels or {}
    roles = ",".join(
        label.split("/")[-1]
        for label in labels
        if label.startswith("node-role.kubernetes.io/") and not label.endswith("/")
    )
    # `node-role.kubernetes.io/master` (legacy) and `node-role.kubernetes.io/control-plane`
    # both surface as role; the split handles them the same way.
    if not roles:
        roles = "<none>"
    return status, roles


def _node_internal_ip(node) -> str:
    """First InternalIP from a Node's status.addresses, or ''."""
    for addr in (node.status.addresses or []):
        if addr.type == "InternalIP" and addr.address:
            return addr.address
    return ""


def _node_taint_summary(node) -> str:
    """Comma-separated `key=value:effect` for every non-duplicate taint."""
    taints = node.spec.taints or []
    if not taints:
        return ""
    parts: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for t in taints:
        key = getattr(t, "key", "") or ""
        value = getattr(t, "value", "") or ""
        effect = getattr(t, "effect", "") or ""
        triple = (key, value, effect)
        if triple in seen:
            continue
        seen.add(triple)
        parts.append(f"{key}={value}:{effect}" if value else f"{key}:{effect}")
    return ",".join(parts)


def list_nodes(
    label_selector: str | None = None,
    include_unschedulable: bool = True,
) -> str:
    """ℹ️ READ — list cluster Nodes with node-specific columns.

    Pick THIS when you want to see scheduling-relevant fields at a glance:
    ROLE / STATUS / INTERNAL_IP / TAINT_SUMMARY / AGE. For the raw object use
    `list_resources(kind="Node", wide=True)` instead — that one returns
    NAME / STATUS / ROLES / AGE / VERSION / INTERNAL_IP / EXTERNAL_IP /
    OS-IMAGE / KERNEL-VERSION / CONTAINER-RUNTIME and is best for capacity
    audits.

    Args:
        label_selector: server-side label filter, e.g. "node-role.kubernetes.io/worker=".
            Applied client-side to keep the apiserver query simple; cheap.
        include_unschedulable: when False, drops `spec.unschedulable=true`
            Nodes (i.e. cordoned). Default True so cordon/drain status is
            visible in the same view.

    Returns a NAME / STATUS / ROLES / INTERNAL_IP / TAINT_SUMMARY / AGE table.
    Empty result returns a helpful notice ("no Nodes matched selector").
    """
    api = _core_v1()
    items = api.list_node(label_selector=label_selector).items

    if not include_unschedulable:
        items = [n for n in items if not (n.spec and n.spec.unschedulable)]

    if not items:
        return (
            "(no Nodes matched the selector)" if label_selector else "(no Nodes found)"
        )

    rows: list[dict[str, str]] = []
    for n in items:
        status, roles = _node_status_summary(n)
        rows.append({
            "NAME": n.metadata.name,
            "STATUS": status,
            "ROLES": roles,
            "INTERNAL_IP": _node_internal_ip(n),
            "TAINT_SUMMARY": _node_taint_summary(n),
            "AGE": format_age(n.metadata.creation_timestamp),
        })
    return short_table(rows, ["NAME", "STATUS", "ROLES", "INTERNAL_IP",
                              "TAINT_SUMMARY", "AGE"])


def label_node(name: str, key: str, value: str | None = None) -> str:
    """⚠️ WRITE — atomic single-label add / update on a Node.

    Equivalent to `kubectl label node <name> <key>=<value>` but touches
    ONLY the targeted label — every other field (status, other labels,
    annotations, taints) is preserved. Mirrors `add_label(kind="Node", ...)`
    for non-Node kinds; this is a Node-specific shortcut so the agent
    doesn't need to remember `api_version="v1"`.

    Args:
        name: node name.
        key: label key (e.g. "workload", "gpu.present"). Keys containing
            `/` or `~` are escaped per RFC 6901 (so
            `node-role.kubernetes.io/worker=` works).
        value: label value. Empty string `""` removes the value but keeps
            the key (rarely useful; prefer `unlabel_node` for removal).
            Omit only when you genuinely want to set a key with no value.

    Raises:
        PermissionError: read-only mode or namespace allowlist doesn't
            include cluster-scoped writes (Node is cluster-scoped; set
            `K8S_MCP_NAMESPACE_ALLOWLIST` to enable cluster-scoped writes
            alongside namespaced ones, otherwise this fails).
    """
    _read_only_guard("label_node")
    settings = get_settings()
    if settings.namespace_allowlist is not None:
        raise PermissionError(
            "Node writes are cluster-scoped and are refused when "
            "K8S_MCP_NAMESPACE_ALLOWLIST is set (use K8S_MCP_READ_ONLY=false "
            "with an unset allowlist for cluster-scoped writes)."
        )
    _validate_node_name(name)
    _validate_label_key(key)
    _validate_label_value(value)
    path = f"/metadata/labels/{_jsonpatch_escape(key)}"
    body = [{"op": "add", "path": path, "value": value if value is not None else ""}]
    try:
        _core_v1().patch_node(name, body, content_type="application/json-patch+json")
    except ApiException as e:
        if e.status == 404:
            raise LookupError(f"Node '{name}' not found") from e
        raise
    rendered = f"{key}={value}" if value is not None else f"{key}="
    return f"Node/{name} label '{rendered}' applied"


def unlabel_node(name: str, key: str) -> str:
    """⚠️ WRITE — atomic single-label remove on a Node.

    Equivalent to `kubectl label node <name> <key>-`. Idempotent: missing
    label = no-op (matches `kubectl label ... <key>-` behavior).

    Args:
        name: node name.
        key: label key to remove. RFC 6901 escaping for `/` and `~`.
    """
    _read_only_guard("unlabel_node")
    settings = get_settings()
    if settings.namespace_allowlist is not None:
        raise PermissionError(
            "Node writes are cluster-scoped and are refused when "
            "K8S_MCP_NAMESPACE_ALLOWLIST is set."
        )
    _validate_node_name(name)
    _validate_label_key(key)
    path = f"/metadata/labels/{_jsonpatch_escape(key)}"
    body = [{"op": "remove", "path": path}]
    try:
        _core_v1().patch_node(name, body, content_type="application/json-patch+json")
    except ApiException as e:
        if e.status == 404:
            raise LookupError(f"Node '{name}' not found") from e
        # JSON Patch `remove` on a missing path returns 422 in some
        # apiserver versions. Treat as idempotent no-op.
        if e.status in (404, 422):
            return f"Node/{name} label '{key}' was not present; no-op"
        raise
    return f"Node/{name} label '{key}' removed"


def taint_node(name: str, taint: str) -> str:
    """⚠️ WRITE — atomic single-taint add on a Node.

    Equivalent to `kubectl taint node <name> <taint-spec>` for a single
    taint. `taint` format: `key=value:effect` (or `key:effect` to set
    an empty value).

    Effects: NoSchedule / PreferNoSchedule / NoExecute. Unknown effects
    are refused client-side before the apiserver round-trip.

    Args:
        name: node name.
        taint: the taint spec, e.g. "dedicated=ml:NoSchedule" or
            "node.kubernetes.io/unreachable:NoExecute".
    """
    _read_only_guard("taint_node")
    settings = get_settings()
    if settings.namespace_allowlist is not None:
        raise PermissionError(
            "Node writes are cluster-scoped and are refused when "
            "K8S_MCP_NAMESPACE_ALLOWLIST is set."
        )
    _validate_node_name(name)
    key, value, effect = _parse_taint_spec(taint)
    body = [
        {
            "op": "add",
            "path": "/spec/taints/-",
            "value": {"key": key, "value": value, "effect": effect},
        }
    ]
    try:
        _core_v1().patch_node(name, body, content_type="application/json-patch+json")
    except ApiException as e:
        if e.status == 404:
            raise LookupError(f"Node '{name}' not found") from e
        raise
    rendered = f"{key}={value}:{effect}" if value else f"{key}:{effect}"
    return f"Node/{name} taint '{rendered}' added"


def untaint_node(name: str, taint: str | None = None) -> str:
    """⚠️ WRITE — remove a single taint (or all taints) from a Node.

    Equivalent to `kubectl taint node <name> <taint-spec>-` for the
    specific-spec path; pass `taint=None` to wipe every taint on the
    node (destructive — use with care).

    Args:
        name: node name.
        taint: the taint spec to remove (same format as `taint_node`). If
            omitted, every taint is removed.
    """
    _read_only_guard("untaint_node")
    settings = get_settings()
    if settings.namespace_allowlist is not None:
        raise PermissionError(
            "Node writes are cluster-scoped and are refused when "
            "K8S_MCP_NAMESPACE_ALLOWLIST is set."
        )
    _validate_node_name(name)

    if taint is None:
        # Wipe every taint — atomic replace with []. The k8s Python
        # client has no `delete_collection` for taints; using JSON Merge
        # Patch with `spec.taints: []` keeps the other spec fields intact.
        try:
            _core_v1().patch_node(name, {"spec": {"taints": []}},
                                  content_type="application/merge-patch+json")
        except ApiException as e:
            if e.status == 404:
                raise LookupError(f"Node '{name}' not found") from e
            raise
        return f"Node/{name} all taints removed"

    key, value, effect = _parse_taint_spec(taint)

    # Read current taints; locate the matching index. JSON Patch `remove`
    # is index-based, so we need the position, not the spec text.
    try:
        node = _core_v1().read_node(name)
    except ApiException as e:
        if e.status == 404:
            raise LookupError(f"Node '{name}' not found") from e
        raise

    current = node.spec.taints or []
    target_idx: int | None = None
    for idx, t in enumerate(current):
        if (t.key == key
                and (t.value or "") == value
                and t.effect == effect):
            target_idx = idx
            break
    if target_idx is None:
        return (
            f"Node/{name} taint '{key}={value}:{effect}' was not present; no-op"
        )

    body = [{"op": "remove", "path": f"/spec/taints/{target_idx}"}]
    _core_v1().patch_node(name, body, content_type="application/json-patch+json")
    rendered = f"{key}={value}:{effect}" if value else f"{key}:{effect}"
    return f"Node/{name} taint '{rendered}' removed"


def register(mcp) -> None:
    mcp.tool()(list_nodes)
    mcp.tool()(label_node)
    mcp.tool()(unlabel_node)
    mcp.tool()(taint_node)
    mcp.tool()(untaint_node)
    mcp.tool()(cordon_node)
    mcp.tool()(uncordon_node)
    mcp.tool()(drain_node)
