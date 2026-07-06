"""Pod explainer — trace owner chain + render sibling / template view.

Why one more tool when kubectl already does it? `kubectl describe pod`
shows the spec, but you usually want the question answered at the next
level up: *which Deployment does this pod belong to, and what are its
siblings doing?* This tool reads `ownerReferences`, walks them up to
the top controller (Deployment / StatefulSet / DaemonSet / CronJob /
ReplicaSet…), and surfaces the answers in a single section per concern.
"""
from __future__ import annotations

import logging

from kubernetes import dynamic
from kubernetes.client.rest import ApiException

from ..client import get_api_client
from ..formatters import short_table
from . import generic as generic_mod

logger = logging.getLogger(__name__)

_MAX_OWNER_HOPS = 8

# Indirection so tests can monkeypatch this namespace without patching
# `generic_mod` itself (which other callers also use).
_generic = generic_mod


def _dyn_client() -> dynamic.DynamicClient:
    return dynamic.DynamicClient(get_api_client())


def _fetch_owner(dc, owner_ref):
    """Resolve one ownerReferences entry into a dict, or None on failure.

    Failure modes we tolerate:
      - Unknown apiVersion+kind (CRD not installed)
      - Resource deleted between when the pod was last reconciled and now
      - RBAC denial reading the owner kind

    We surface the failure in the report; we do not raise, because the
    agent still wants the rest of the chain.
    """
    av = owner_ref.get("apiVersion") or ""
    kind = owner_ref.get("kind") or ""
    name = owner_ref.get("name") or ""
    namespace = owner_ref.get("namespace")  # cluster-scoped owners lack it
    uid = owner_ref.get("uid")
    try:
        res = _generic._resource_for_kind(dc, kind, api_version=av)
    except (ValueError, ApiException) as e:
        return {"_unresolved": True, "kind": kind, "name": name, "apiVersion": av,
                "error": str(e).splitlines()[0]}
    try:
        if namespace:
            obj = res.get(name=name, namespace=namespace)
        else:
            obj = res.get(name=name)
    except ApiException as e:
        if getattr(e, "status", None) == 404 or "not found" in str(e).lower():
            return {"_missing": True, "kind": kind, "name": name}
        return {"_error": True, "kind": kind, "name": name, "error": str(e)}
    obj_dict = _generic._to_dict(obj)
    return {"_obj": obj_dict, "kind": kind, "name": name,
            "apiVersion": av, "namespace": namespace, "uid": uid}


def _format_chain_label(chain):
    """'Pod → ReplicaSet → Deployment'."""
    return " → ".join(c["kind"] for c in chain)


def _sibling_pods(dc, label_selector, namespace="default"):
    """Sibling-pod lookup, separate from the main fetcher for testability.

    Returns plain dicts (mimicking what DynamicClient gives) or {} on
    failure. The `dc` parameter is currently unused (we go via the
    kubernetes client CoreV1Api to keep the selector handling native),
    but it's part of the signature so a caller can swap it for a stub.
    """
    try:
        from kubernetes import client
        api = client.CoreV1Api(get_api_client())
        kvs = ",".join(f"{k}={v}" for k, v in label_selector.items())
        items = api.list_namespaced_pod(
            namespace, label_selector=kvs,
        ).items
        return [
            {"metadata": {"name": p.metadata.name}, "spec": {"nodeName": p.spec.node_name},
             "status": {"phase": p.status.phase}}
            for p in items
        ]
    except Exception as e:
        logger.warning("sibling listing failed: %s", e)
        return []


# Backward-compat alias for tests written against the inline name.
_pods_in_namespace = _sibling_pods


def explain_pod(namespace: str, name: str) -> str:
    """🧭 POD explainer — top-down view: who owns me, who are my siblings.

    Aggregates what an agent otherwise has to chain by hand:
      - the **owner chain** walked via `metadata.ownerReferences` up to
        the top controller (Stops at Deployment / StatefulSet /
        DaemonSet / Job, or earlier if the chain dead-ends). Each
        hop shows kind / name / uid so you can verify it's the
        object you expect.
      - the **sibling set** — pods sharing the top controller's
        labels (Pods-with-same-pod-template-hash for ReplicaSet /
        Deployment, etc.) — useful for "is this pod alone, or are
        its siblings doing the same thing?"
      - the **Pod spec essentials** — node, serviceAccount, the
        container list with image refs. Complements `diagnose_pod`
        (which focuses on runtime) — this one focuses on static
        ownership + scheduled placement.

    Tolerates dangling / unknown owners without raising: if a
    referenced CRD is gone or unreadable, the chain displays the
    hole ("could not resolve ReplicaSet/foo") and stops walking.

    Read-only. Uses DynamicClient, so CRDs are walked the same way
    built-ins are.
    """
    dc = _dyn_client()
    try:
        pod_res = _generic._resource_for_kind(dc, "Pod", api_version="v1")
        pod_obj = pod_res.get(name=name, namespace=namespace)
    except ValueError as e:
        raise RuntimeError(f"Kubernetes API unreachable: {e}") from e
    except ApiException as e:
        if getattr(e, "status", None) == 404 or "not found" in str(e).lower():
            raise ValueError(f"pod {namespace}/{name} not found") from e
        raise

    pod_dict = _generic._to_dict(pod_obj)
    pod_meta = pod_dict.get("metadata") or {}
    pod_spec = pod_dict.get("spec") or {}

    pod_uid = pod_meta.get("uid", "")
    pod_labels = pod_meta.get("labels") or {}
    pod_node = pod_spec.get("nodeName") or ""
    pod_sa = pod_spec.get("serviceAccountName") or "default"
    pod_containers = pod_spec.get("containers") or []

    lines = [
        f"## Pod explanation: {namespace}/{name}",
        f"UID: {pod_uid}",
        f"Node: {pod_node or '(unscheduled)'}",
        f"ServiceAccount: {pod_sa}",
        f"Labels: {', '.join(f'{k}={v}' for k, v in pod_labels.items()) or '(none)'}",
        "",
    ]

    # ---------- owner chain -------------------------------------------------
    chain = [{"kind": "Pod", "name": name, "uid": pod_uid, "apiVersion": "v1",
              "namespace": namespace, "_obj": pod_dict}]
    owners = pod_meta.get("ownerReferences") or []
    if not owners:
        lines.append("### Owner chain")
        lines.append("(no ownerReferences — pod is not managed by any controller)")
        lines.append("")
    else:
        seen_uids = {pod_uid}
        hop = 0
        current_owners = owners
        while current_owners and hop < _MAX_OWNER_HOPS:
            ref = current_owners[0]  # Controller owner; Pods typically have 1
            entry = _fetch_owner(dc, ref)
            chain.append(entry)
            if entry.get("_unresolved") or entry.get("_missing") or entry.get("_error"):
                break
            obj_dict = entry["_obj"]
            obj_meta = obj_dict.get("metadata") or {}
            if obj_meta.get("uid") in seen_uids:
                # Cycle protection — should not happen with sane clusters
                break
            seen_uids.add(obj_meta.get("uid", ""))
            current_owners = obj_meta.get("ownerReferences") or []
            hop += 1

        lines.append("### Owner chain")
        lines.append(_format_chain_label(chain))
        for i, hop_dict in enumerate(chain[1:], start=2):
            if hop_dict.get("_unresolved"):
                lines.append(
                    f"  {i}. {hop_dict['kind']}/{hop_dict['name']} "
                    f"(apiVersion={hop_dict['apiVersion']}) — "
                    f"could not resolve: {hop_dict.get('error', '?')}"
                )
            elif hop_dict.get("_missing"):
                lines.append(
                    f"  {i}. {hop_dict['kind']}/{hop_dict['name']} "
                    f"— gone (deleted?)"
                )
            elif hop_dict.get("_error"):
                lines.append(
                    f"  {i}. {hop_dict['kind']}/{hop_dict['name']} "
                    f"— error: {hop_dict.get('error', '?')}"
                )
            else:
                uid = hop_dict.get("uid") or ""
                uid_short = uid[:8] + "…" if len(uid) > 8 else uid
                lines.append(
                    f"  {i}. {hop_dict['kind']}/{hop_dict['name']} "
                    f"(uid={uid_short or '?'})"
                )
        lines.append("")

    # ---------- top controller metadata -------------------------------------
    # chain[0] is always the Pod itself. If only that entry exists,
    # there is no top controller — skip the section entirely.
    top = chain[-1] if len(chain) > 1 else None
    if top is not None and not (top.get("_unresolved") or top.get("_missing") or top.get("_error")):
        top_spec = (top.get("_obj") or {}).get("spec") or {}
        top_status = (top.get("_obj") or {}).get("status") or {}
        replicas = (
            top_spec.get("replicas") or top_status.get("replicas") or "?"
        )
        lines.append("### Top controller")
        lines.append(f"{top['kind']}/{top['name']} — replicas: {replicas}")
        lines.append("")

    # ---------- sibling pods ------------------------------------------------
    siblings = []
    if top is not None and not (top.get("_unresolved") or top.get("_missing") or top.get("_error")):
        # Use the parent object's pod-template labels if it has them
        # (Deployment.spec.template.metadata.labels) — that's what
        # identifies a sibling set for ReplicaSet→Deployment chains.
        # For a direct-ReplicaSet owner, fall back to its labels.
        parent_spec = (top.get("_obj") or {}).get("spec") or {}
        tmpl_labels = (
            parent_spec.get("template", {}).get("metadata", {}).get("labels", {})
        )
        parent_labels = ((top.get("_obj") or {}).get("metadata", {}) or {}).get(
            "labels", {}
        )
        selector = tmpl_labels or parent_labels
        if selector:
            sib_dicts = _sibling_pods(dc, selector, namespace)
            siblings = [
                {"NAME": (s.get("metadata") or {}).get("name", "?"),
                 "NODE": (s.get("spec") or {}).get("nodeName", ""),
                 "PHASE": (s.get("status") or {}).get("phase", "")}
                for s in sib_dicts
            ]
    if siblings:
        lines.append(
            f"### Sibling pods (same controller, n={len(siblings)})"
        )
        # Show this pod first, then by name
        siblings.sort(key=lambda r: (0 if r["NAME"] == name else 1, r["NAME"]))
        lines.append(short_table(siblings, ["NAME", "PHASE", "NODE"]))
        lines.append("")

    # ---------- pod spec essentials ----------------------------------------
    lines.append("### Containers")
    if pod_containers:
        for c in pod_containers:
            img = c.get("image", "?")
            lines.append(f"- **{c.get('name', '?')}** — image: `{img}`")
    else:
        lines.append("(no containers declared)")
    lines.append("")

    return "\n".join(lines).rstrip()


def register(mcp) -> None:
    mcp.tool()(explain_pod)
