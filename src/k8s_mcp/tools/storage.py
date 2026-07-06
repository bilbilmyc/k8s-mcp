"""Storage: create_pvc, delete_pvc, bulk_delete_pvc, and the local-path bootstrap.

`create_pvc` claims a single PersistentVolume. When the cluster has no
StorageClass at all, that PVC will sit Pending forever — most dev/test
clusters (kind, k3s default, minikube with no extra setup) hit this. The
escape hatch is `bootstrap_local_path_provisioner`: it applies Rancher's
local-path-storage manifest in one shot, giving the cluster a working
`local-path` StorageClass.

`delete_pvc` is a one-step (no two-step HMAC) delete. PVCs are
declarative — deleting one does not cascade-delete workloads; the
workload just goes Pending until a replacement is bound. Recoverable
by re-running `create_pvc` with the same name.

`bulk_delete_pvc` is the label-selector-driven batch version. Goes
through the same `dry_run → token → confirm` flow as the rest of the
bulk_* family (see bulk.py), so new PVCs appearing with the same
label_selector between preview and confirm are NOT touched.

中文说明：
- `create_pvc`：单个 PVC 声明，集群必须已有对应 StorageClass 才能绑定。
- `delete_pvc`：一步删除（不强制两段式 HMAC），PVC 是声明性资源，
  删了不会级联，工作负载只会 Pending 等待重新绑定。
- `bulk_delete_pvc`：按 label_selector 批量删 PVC，走 dry_run → token →
  confirm 三段式（与 bulk.py 一致）。
- `bootstrap_local_path_provisioner`：在 SC 缺失时一次性 install 一个
  hostPath-based 的本地 provisioner（等价于
  `kubectl apply -f rancher/local-path-storage.yaml`）。
"""
from __future__ import annotations

import logging
import urllib.error
import urllib.request

import yaml
from kubernetes import client
from kubernetes.client.rest import ApiException

from ..client import get_api_client, get_caller_identity  # noqa: F401  (used by tests indirectly)
from ..config import enforce_write_safety, get_settings
from ..formatters import short_table
from ..safety import TokenError, issue_token, verify_token
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


def delete_pvc(name: str, namespace: str) -> str:
    """⚠️ WRITE — delete a PVC (one-step, no two-step HMAC).

    Why one-step: PVC deletion is recoverable — re-running `create_pvc`
    with the same name brings it back. Deleting a PVC also does NOT
    cascade-delete workloads that mount it; the workload just stays
    Pending until a replacement PVC is bound. This makes it safer than
    the generic two-step `delete_resource(kind="PersistentVolumeClaim", ...)`.

    For multi-PVC cleanup by label, use `bulk_delete_pvc` instead.

    .. deprecated::
        Use :func:`delete_resource` with ``kind='PersistentVolumeClaim'``
        instead. This one-step wrapper will be removed in v0.5.0;
        the two-step preview+confirm flow is the recommended path for
        all destructive ops going forward.

    Args:
        name: PVC name, or a list of PVC names to delete serially.
        namespace: PVC namespace.
    """
    settings = get_settings()
    if settings.read_only:
        raise PermissionError(
            "Server is in read-only mode (K8S_MCP_READ_ONLY=true). "
            "delete_pvc is disabled."
        )
    if not settings.ns_allowed(namespace):
        raise PermissionError(
            f"Delete in namespace '{namespace}' is not allowed by "
            "K8S_MCP_NAMESPACE_ALLOWLIST"
        )
    names = name if isinstance(name, list) else [name]
    rows: list[str] = []
    for n in names:
        try:
            _core_v1().delete_namespaced_persistent_volume_claim(n, namespace)
        except ApiException as e:
            if e.status == 404:
                raise LookupError(f"PVC '{namespace}/{n}' not found") from e
            raise
        rows.append(
            f"⚠️ DEPRECATED: delete_pvc will be removed in v0.5.0 — "
            f"use delete_resource(kind='PersistentVolumeClaim') for the audited two-step flow.\n"
            f"PVC/{namespace}/{n} deleted"
        )
    return "\n".join(rows)


# ---------- bulk_delete_pvc --------------------------------------------------


def _list_matched_pvcs(
    namespace: str | None, label_selector: str,
) -> list[dict]:
    """Return PVCs matching the selector, as plain dicts."""
    api = _core_v1()
    get_kwargs: dict = {"label_selector": label_selector}
    if namespace:
        ret = api.list_namespaced_persistent_volume_claim(namespace, **get_kwargs)
    else:
        ret = api.list_persistent_volume_claim_for_all_namespaces(**get_kwargs)
    out = []
    for item in ret.items:
        obj = generic._to_dict(item)
        if hasattr(obj, "to_dict"):
            obj = obj.to_dict()
        # The CoreV1Api returns V1PersistentVolumeClaim objects; convert.
        if not isinstance(obj, dict):
            obj = {
                "metadata": {
                    "name": item.metadata.name,
                    "namespace": item.metadata.namespace,
                },
                "spec": {"volumeName": item.spec.volume_name} if item.spec.volume_name else {},
                "status": {"phase": item.status.phase} if item.status else {},
            }
        out.append(obj)
    return out


def bulk_delete_pvc(
    label_selector: str,
    namespace: str | None = None,
    dry_run: bool = True,
    confirm: bool = False,
    confirmation_token: str | None = None,
) -> str:
    """⚠️ WRITE — delete every PVC matching `label_selector`. Same
    `dry_run → token → confirm` flow as the workload bulk_* tools.

    Use case: cleanup of orphan PVCs (e.g. from a deleted StatefulSet)
    where a label like `app=postgres` is still on the orphaned PVCs.

    Safety flow (identical to bulk_set_image / bulk_restart / bulk_scale):
      1. `dry_run=True` (default): list matches, no write, no token.
      2. `dry_run=False, confirm=False`: re-list, render the same preview,
         issue an HMAC-signed `confirmation_token` (5-min TTL).
      3. `dry_run=False, confirm=True, confirmation_token=...`: verify
         the token, then delete ONLY the resources that were matched at
         preview time. New PVCs matching the same label_selector
         between preview and confirm are NOT touched.

    .. deprecated::
        Use :func:`delete_pvc` with a list of names passed as `name`
        (no label_selector, no dry_run / confirm flow — one-step per
        PVC). For audited label-selector-based bulk delete, await
        v0.5.0's two-step `delete_resource` integration.

    Args:
        label_selector: e.g. "app=postgres". Required.
        namespace: limit to one namespace; None = all namespaces.
        dry_run / confirm / confirmation_token: see flow above.
    """
    return _deprecate_pvc(
        _bulk_delete_pvc_impl(
            label_selector, namespace, dry_run, confirm, confirmation_token,
        )
    )


def _deprecate_pvc(body: str) -> str:
    """Prepend the deprecation marker for `bulk_delete_pvc`.

    Lives in storage.py (not bulk.py) because `bulk_delete_pvc` already
    lived in storage.py pre-consolidation. The marker string matches
    the one in bulk.py so an agent that sees one migration notice sees
    all of them in the same shape.
    """
    note = (
        "⚠️ DEPRECATED: bulk_delete_pvc will be removed in v0.5.0 — "
        "pass a list of names to delete_pvc instead. For label_selector-based "
        "operations with the audited dry_run → confirm flow, keep using this "
        "tool until v0.5.0."
    )
    return f"{note}\n{body}"


def _bulk_delete_pvc_impl(
    label_selector: str,
    namespace: str | None = None,
    dry_run: bool = True,
    confirm: bool = False,
    confirmation_token: str | None = None,
) -> str:
    """Internal body of `bulk_delete_pvc` — kept separate so the public
    function can prepend the deprecation marker without tangling the
    label_selector / dry_run / confirm flow."""
    if not label_selector:
        raise ValueError("label_selector is required for bulk_delete_pvc")

    settings = get_settings()
    if settings.read_only:
        raise PermissionError(
            "Server is in read-only mode (K8S_MCP_READ_ONLY=true). "
            "bulk_delete_pvc is disabled."
        )
    enforce_write_safety(settings)
    if not settings.ns_allowed(namespace):
        raise PermissionError(
            f"Namespace {namespace!r} not allowed by K8S_MCP_NAMESPACE_ALLOWLIST"
        )

    matched = _list_matched_pvcs(namespace, label_selector)
    plans = []
    for item in matched:
        md = item.get("metadata", {}) or {}
        spec = item.get("spec", {}) or {}
        status = item.get("status", {}) or {}
        plans.append({
            "NAMESPACE": md.get("namespace", ""),
            "NAME": md.get("name", "?"),
            "PHASE": status.get("phase", "?"),
            "VOLUME": spec.get("volumeName") or "<dynamic>",
        })

    ns_part = f" in namespace {namespace!r}" if namespace else " cluster-wide"
    header = (
        f"bulk_delete_pvc — label_selector={label_selector!r}{ns_part}\n"
        f"Matched {len(plans)} PVC(s):"
    )
    body = short_table(plans, list(plans[0].keys())) if plans else "(no PVCs)"

    if dry_run:
        return (
            f"{header}\n{body}\n\n"
            f"Re-call with dry_run=False, confirm=False to get a token; "
            f"then dry_run=False, confirm=True + token to delete."
        )

    if not confirm:
        caller = get_caller_identity()
        token = issue_token(
            {
                "op": "bulk_delete_pvc",
                "label_selector": label_selector,
                "namespace": namespace or "",
                "matched_names": [(p["NAMESPACE"], p["NAME"]) for p in plans],
                "caller": {
                    "username": caller.get("username", "(unknown)"),
                    "uid": caller.get("uid", ""),
                },
            },
            settings.delete_token_secret,
            settings.delete_token_ttl_seconds,
        )
        return (
            f"{header}\n{body}\n\n"
            f"confirmation_token (HMAC-signed, "
            f"{settings.delete_token_ttl_seconds}s TTL):\n{token}\n\n"
            f"To delete, re-call with dry_run=False, confirm=True and the "
            f"token above. The delete will apply ONLY to the {len(plans)} "
            f"PVC(s) listed."
        )

    try:
        payload = verify_token(confirmation_token or "", settings.delete_token_secret)
    except TokenError:
        raise
    if payload.get("op") != "bulk_delete_pvc":
        raise TokenError(
            f"Token was issued for op={payload.get('op')!r}, "
            f"but you called bulk_delete_pvc."
        )
    if payload.get("label_selector") != label_selector:
        raise TokenError("Token label_selector does not match this call")
    if (payload.get("namespace") or "") != (namespace or ""):
        raise TokenError("Token namespace does not match this call")
    # Caller binding check — same defense-in-depth as bulk._verify_bulk_token
    caller = get_caller_identity()
    token_caller = payload.get("caller") or {}
    if token_caller.get("username", "") != caller.get("username", ""):
        raise TokenError(
            f"Token caller mismatch: issued for "
            f"{token_caller.get('username')!r}, current server runs as "
            f"{caller.get('username')!r}. A leaked token cannot be "
            f"replayed across MCP servers with different identities."
        )
    if token_caller.get("uid", "") != caller.get("uid", ""):
        raise TokenError(
            "Token caller UID mismatch — same username but different "
            "underlying identity (token replay across distinct SAs?)"
        )

    matched_set = {tuple(p) for p in payload.get("matched_names", [])}
    api = _core_v1()
    rows = []
    for ns, name in sorted(matched_set):
        try:
            api.delete_namespaced_persistent_volume_claim(name, ns)
            rows.append({"NAMESPACE": ns, "NAME": name, "RESULT": "deleted"})
        except ApiException as e:
            if e.status == 404:
                rows.append({
                    "NAMESPACE": ns, "NAME": name,
                    "RESULT": "SKIPPED (already gone)",
                })
            else:
                rows.append({
                    "NAMESPACE": ns, "NAME": name,
                    "RESULT": f"ERROR: {e.reason} (status {e.status})",
                })
    deleted = sum(1 for r in rows if r["RESULT"] == "deleted")
    skipped = sum(1 for r in rows if r["RESULT"].startswith("SKIPPED"))
    errs = sum(1 for r in rows if r["RESULT"].startswith("ERROR"))
    summary = f"bulk_delete_pvc — deleted {deleted}/{len(rows)} PVC(s)"
    if skipped:
        summary += f" ({skipped} skipped — already gone)"
    if errs:
        summary += f" ({errs} errors)"
    return summary + "\n" + short_table(rows, ["NAMESPACE", "NAME", "RESULT"])


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
        Override via `K8S_MCP_LOCAL_PATH_PROVISIONER_URL` for air-gapped
        clusters with an internal mirror.
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
    mcp.tool()(delete_pvc)
    mcp.tool()(bulk_delete_pvc)
    mcp.tool()(bootstrap_local_path_provisioner)
    mcp.tool()(validate_pv_hostpath_paths)
