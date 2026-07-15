"""Storage: PVC creation and the local-path provisioner bootstrap.

`create_pvc` claims a single PersistentVolume. When the cluster has no
StorageClass at all, that PVC will sit Pending forever — most dev/test
clusters (kind, k3s default, minikube with no extra setup) hit this. The
escape hatch is `bootstrap_local_path_provisioner`: it applies Rancher's
local-path-storage manifest in one shot, giving the cluster a working
`local-path` StorageClass.

The audited two-step general-purpose delete path lives in
`delete_resource(kind="PersistentVolumeClaim", ...)`. The previous
one-step `delete_pvc` and label-selector `bulk_delete_pvc` were
deprecated in v0.4.x and removed in v0.5.0 — both were subsumed by the
audited flow.

中文说明：
- `create_pvc`：单个 PVC 声明，集群必须已有对应 StorageClass 才能绑定。
- `bootstrap_local_path_provisioner`：在 SC 缺失时一次性 install 一个
  hostPath-based 的本地 provisioner（等价于
  `kubectl apply -f rancher/local-path-storage.yaml`）。
- `validate_pv_hostpath_paths`：列出集群中 hostPath PV 实际指向的
  主机目录与节点名，标出 kubelet 启动前需先 `mkdir -p` 的位置。
"""
from __future__ import annotations

import logging
import urllib.error
import urllib.request

import yaml
from kubernetes import client

from ..client import get_api_client
from ..config import get_settings
from . import generic

logger = logging.getLogger(__name__)


# Default-annotation key Rancher uses. We grep to remove when the user
# asks NOT to mark this SC as the cluster default.
_DEFAULT_CLASS_ANNOTATION = (
    'storageclass.kubernetes.io/is-default-class: "true"'
)


# Module-level manifest cache. k8s-mcp's session lifetime is one MCP
# connection, so we don't bother invalidating; restart_clears_state
# covers cross-session reloads.
_manifest_cache: str | None = None


def _read_only_guard(action: str) -> None:
    if get_settings().read_only:
        raise PermissionError(
            f"Server is in read-only mode (K8S_MCP_READ_ONLY=true). "
            f"{action} is disabled."
        )


def create_pvc(
    name: str,
    namespace: str,
    size: str,
    access_modes: list[str] | None = None,
    storage_class: str | None = None,
    volume_name: str | None = None,
    labels: dict[str, str] | None = None,
) -> str:
    """⚠️ WRITE / ⚠️ PROVISIONS STORAGE — claims a PersistentVolume from the
    cluster. On cloud providers this is a billable resource (GB-month cost);
    ensure the size and storage_class are right before confirming with the
    user.

    Args:
        name: PVC name.
        namespace: target namespace.
        size: requested size, e.g. "1Gi", "10Gi".
        access_modes: defaults to ["ReadWriteOnce"]. Pass a list like
            ["ReadOnlyMany", "ReadWriteMany"] for ROX/RWX filesystems.
        storage_class: optional StorageClass name. If the cluster has no
            StorageClass at all, run `bootstrap_local_path_provisioner()`
            first to give it one.
        volume_name: pin the PVC to a specific PersistentVolume by name
            (sets `spec.volumeName`). Use this when binding to a
            pre-provisioned local PV (hostPath / local) on a dev/test
            cluster that has no dynamic provisioner — the PVC must name
            the PV explicitly, otherwise it sits Pending forever.
        labels: optional labels.

    NOTE on hostPath PVs: if `volume_name` points to a hostPath PV, the
    kubelet does NOT create the host directory — it must already exist
    on the target Node. When this tool detects that, the result includes
    a `mkdir -p` hint; run it on the node (or call
    `validate_pv_hostpath_paths` first) before scheduling a Pod that
    mounts the PVC.
    """
    if access_modes is None:
        access_modes = ["ReadWriteOnce"]
    md: dict = {"name": name, "namespace": namespace}
    if labels:
        md["labels"] = labels
    spec = {
        "accessModes": access_modes,
        "resources": {"requests": {"storage": size}},
    }
    if storage_class:
        spec["storageClassName"] = storage_class
    if volume_name:
        spec["volumeName"] = volume_name
    manifest = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": md,
        "spec": spec,
    }
    result = generic.apply_yaml(yaml.safe_dump(manifest))
    if volume_name:
        hint = _hostpath_pv_hint(volume_name)
        if hint:
            return f"{result}\n\n{hint}"
    return result


def _node_for_pv(pv_obj: dict) -> str | None:
    """Best-effort hostname for a PV — nodeAffinity first, else None."""
    affinity = (pv_obj.get("spec", {}) or {}).get("nodeAffinity", {}) or {}
    terms = affinity.get("required", {}).get("nodeSelectorTerms", []) or []
    for term in terms:
        for expr in term.get("matchExpressions", []) or []:
            if (expr.get("key") == "kubernetes.io/hostname"
                    and expr.get("operator") == "In"
                    and expr.get("values")):
                return expr["values"][0]
    return None


def _core_v1():
    return client.CoreV1Api(get_api_client())


def _hostpath_pv_hint(pv_name: str) -> str:
    """If PV <pv_name> is hostPath type, return a `mkdir` hint naming the
    target node and host path. Return '' if the PV is not hostPath or
    could not be fetched (don't fail the create call on a hint lookup)."""
    try:
        pv = generic.get_resource(kind="PersistentVolume", name=pv_name)
    except Exception as e:  # noqa: BLE001
        logger.debug("hostPath hint skipped: PV %s lookup failed: %s", pv_name, e)
        return ""
    spec = pv.get("spec", {}) or {}
    host_path = spec.get("hostPath")
    if not host_path:
        return ""
    path = host_path.get("path", "<unknown>")
    node = _node_for_pv(pv) or "<the node that mounts this PV>"
    typ = host_path.get("type", "DirectoryOrCreate")
    typ_note = ""
    if typ != "DirectoryOrCreate":
        typ_note = (
            f"  (hostPath.type={typ} — directory must already exist; "
            f"DirectoryOrCreate would auto-create it.)\n"
        )
    return (
        f"⚠️  PV '{pv_name}' is hostPath type — the kubelet does NOT create "
        f"the directory on the node. Before the Pod can mount, run on node "
        f"'{node}':\n"
        f"    mkdir -p {path!r}\n"
        f"{typ_note}"
        f"  SSH:  ssh {node} 'mkdir -p {path!r}'\n"
        f"  Verify:  ssh {node} 'ls -ld {path!r}'\n"
        f"Without this, Pods will hit FailedMount: path ... does not exist. "
        f"Call `validate_pv_hostpath_paths()` to see all hostPath PVs at once."
    )


def bootstrap_local_path_provisioner(
    set_as_default: bool = True,
    apply_immediately: bool = True,
) -> str:
    """⚠️ WRITE — install Rancher local-path-provisioner in one shot.
    Solves "my cluster has no StorageClass, so PVCs sit Pending forever"
    on dev/test clusters (kind, k3s default, minikube with no extras).

    The manifest creates:
      - a privileged DaemonSet (`local-path-provisioner`) on every node
      - a `local-path` StorageClass with `volumeBindingMode=WaitForFirstConsumer`
      - the RBAC it needs

    PVCs submitted with `storage_class_name="local-path"` are then
    auto-provisioned onto node hostPath storage. Production clusters
    should NOT use this — hostPath PVCs are node-local and data is
    lost if the node dies.

    Args:
        set_as_default: when True (default), mark the StorageClass as the
            cluster's default so callers can omit `storage_class_name`.
            Pass False if you already have a default SC and want both
            to coexist.
        apply_immediately: when True (default), runs `apply_yaml` to
            create all the cluster resources. Pass False to return the
            raw YAML for the user to inspect before applying.

    Manifest URL:
        Defaults to the reviewed Rancher v0.0.32 manifest. Override via
        `K8S_MCP_LOCAL_PATH_PROVISIONER_URL` for air-gapped clusters with an
        internally reviewed mirror.
    """
    _read_only_guard("bootstrap_local_path_provisioner")
    yaml_text = _fetch_local_path_manifest()

    if not set_as_default:
        # Strip the default annotation so the cluster's existing default
        # stays default. The annotation's value form is YAML-string so we
        # match the exact key Rancher ships.
        yaml_text = yaml_text.replace(_DEFAULT_CLASS_ANNOTATION,
                                      _DEFAULT_CLASS_ANNOTATION.replace("true", "false"))

    if not apply_immediately:
        return (
            f"Local Path Provisioner manifest (NOT applied; "
            f"set_as_default={set_as_default}):\n"
            f"----\n{yaml_text}----\n"
            f"Re-run with apply_immediately=True to install."
        )

    result = generic.apply_yaml(yaml_text)
    sc_state = "default" if set_as_default else "non-default"
    return (
        f"{result}\n\n"
        f"Local Path Provisioner installed (StorageClass 'local-path', {sc_state}). "
        f"You can now create PVCs / StatefulSets with "
        f"storage_class_name='local-path' (or omit — {sc_state})."
    )


def _fetch_local_path_manifest() -> str:
    """Fetch + cache the local-path-provisioner manifest. Module-level
    cache survives within one MCP session; restarts re-fetch.

    Raises RuntimeError with an actionable message on network/parse
    failure — the cluster may be air-gapped; in that case the user
    can paste `K8S_MCP_LOCAL_PATH_PROVISIONER_URL` pointing at an
    internal mirror.
    """
    global _manifest_cache
    if _manifest_cache is not None:
        return _manifest_cache
    url = get_settings().local_path_provisioner_url
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "k8s-mcp/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(
            f"Could not fetch local-path-provisioner manifest from {url!r}: {e}. "
            f"If the cluster is air-gapped, set "
            f"K8S_MCP_LOCAL_PATH_PROVISIONER_URL=https://your-mirror/path/to/local-path-storage.yaml "
            f"and retry. Or pre-install manually with "
            f"`kubectl apply -f {url}` and skip this tool."
        ) from e
    if not text.strip():
        raise RuntimeError(
            f"local-path-provisioner manifest at {url!r} came back empty"
        )
    _manifest_cache = text
    return text


# ---------- hostPath PV diagnostics -----------------------------------------


def _list_hostpath_pvs() -> list[dict]:
    """Return normalized records for every hostPath PV in the cluster.

    Each record: {name, capacity, claim_ns, claim_name, path, type, node}.
    `claim_ns`/`claim_name` are empty strings if PV is unbound; `node` is
    the hostname from `nodeAffinity` (best effort) or '' when unknown.
    """
    dc = generic._dyn_client()
    resource = generic._resource_for_kind(dc, "PersistentVolume")
    ret = resource.get()  # cluster-scoped
    out: list[dict] = []
    for item in ret.items:
        obj = generic._to_dict(item)
        spec = obj.get("spec", {}) or {}
        hp = spec.get("hostPath")
        if not hp:
            continue
        claim_ref = spec.get("claimRef", {}) or {}
        out.append({
            "name": (obj.get("metadata", {}) or {}).get("name", "?"),
            "capacity": (spec.get("capacity", {}) or {}).get("storage", "?"),
            "claim_ns": claim_ref.get("namespace", "") or "",
            "claim_name": claim_ref.get("name", "") or "",
            "path": hp.get("path", "<unknown>"),
            "type": hp.get("type", "DirectoryOrCreate"),
            "node": _node_for_pv(obj) or "",
        })
    return out


def _format_hostpath_pv_report(host_pvs: list[dict]) -> str:
    """Render the hostPath PV list into a CLI-friendly report with a
    ready-to-paste `ssh` command for each entry.

    Separated from `_list_hostpath_pvs` so the formatter can be unit-tested
    without touching the apiserver.
    """
    if not host_pvs:
        return (
            "No hostPath PVs found. If PVCs sit Pending, the issue is "
            "elsewhere (StorageClass missing, capacity, selector, etc.) — "
            "try `get_resource(kind='Pod', ...)` to read the FailedScheduling "
            "events."
        )
    lines = [f"Found {len(host_pvs)} hostPath PV(s) — hostPath volumes do "
             f"NOT auto-create their host directory:", ""]
    for pv in host_pvs:
        bound_to = ""
        if pv["claim_ns"] and pv["claim_name"]:
            bound_to = f"  →  bound to {pv['claim_ns']}/{pv['claim_name']}"
        node = pv["node"] or "<unknown — see PV nodeAffinity or bound Pod>"
        lines.append(
            f"  • {pv['name']}  ({pv['capacity']}){bound_to}\n"
            f"      node:    {node}\n"
            f"      path:    {pv['path']}\n"
            f"      type:    {pv['type']}"
            + ("  (NOT DirectoryOrCreate — directory must already exist)"
               if pv["type"] != "DirectoryOrCreate" else "")
            + "\n"
            f"      check:   ssh {node} 'ls -ld {pv['path']!r} 2>/dev/null \\\n"
            f"                  || echo MISSING; sudo mkdir -p {pv['path']!r}'\n"
        )
    return "\n".join(lines)


def validate_pv_hostpath_paths() -> str:
    """List every hostPath PV with the host path it expects, the node it's
    pinned to, and a ready-to-paste `ssh` command to verify (and create) the
    directory.

    Background — a PV with `spec.hostPath.path=/foo` does NOT make the
    kubelet create `/foo` on the node. If the directory is missing, kubelet
    reports `FailedMount: path ... does not exist` and the Pod stays
    ContainerCreating / Pending. The fix is `mkdir -p <path>` on the target
    node BEFORE the Pod is scheduled.

    This tool does not run commands on the node itself (k8s-mcp is not an
    SSH client). It prints the exact `ssh` command the operator should run
    for each hostPath PV, plus a `check` form that prints `MISSING` when the
    directory is absent.

    Typical use: after a StatefulSet / Deployment stays Pending with a
    FailedMount event, call this to see whether the host directory is the
    root cause. If the report lists no hostPath PVs, look elsewhere
    (StorageClass missing, capacity, label selectors, etc.) — the issue is
    not hostPath.

    The host directory requirement also appears inline in
    `create_pvc(volume_name=...)` output as a reminder, so this tool is the
    standalone "see all of them at once" view.
    """
    return _format_hostpath_pv_report(_list_hostpath_pvs())


def register(mcp) -> None:
    mcp.tool()(create_pvc)
    mcp.tool()(bootstrap_local_path_provisioner)
    mcp.tool()(validate_pv_hostpath_paths)
