"""Generic resource tools: list / get / get_yaml / describe / apply.

All use the kubernetes DynamicClient so any registered Kind works.

中文说明：
通用资源工具，覆盖"读、查、检视、改"四大类核心动作，全部走
kubernetes DynamicClient，所以内置 kind 与 CRD 都能用。其中：

  - `apply_yaml`：服务端对比（CREATE vs UPDATE）；PUT 时通过
    ResourceVersion 防止覆盖他人更新。
  - `diff_resource`：在 apply 之前预览差异，让 Agent 给用户看。
  - `get_resource_yaml` 默认对 Secret 做脱敏；需 `reveal_secrets=True`
    才输出原文。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC
from typing import Any

import yaml
from kubernetes import dynamic
from kubernetes.client.rest import ApiException
from kubernetes.dynamic.exceptions import ResourceNotFoundError, ResourceNotUniqueError

from ..client import get_api_client
from ..config import get_settings
from ..formatters import describe as describe_fmt
from ..formatters import mask_secret_data, short_table, to_yaml

logger = logging.getLogger(__name__)


def _dyn_client() -> dynamic.DynamicClient:
    return dynamic.DynamicClient(get_api_client())


def _to_dict(resource: Any) -> dict:
    """Normalize a DynamicClient resource into a plain dict."""
    if hasattr(resource, "to_dict"):
        return resource.to_dict()
    return dict(resource)


def _api_version_for(kind: str) -> str | None:
    """Best-effort api_version lookup for built-in kinds."""
    return {
        "Pod": "v1",
        "Service": "v1",
        "ConfigMap": "v1",
        "Secret": "v1",
        "Namespace": "v1",
        "Node": "v1",
        "PersistentVolume": "v1",
        "PersistentVolumeClaim": "v1",
        "ServiceAccount": "v1",
        "Endpoints": "v1",
        "Event": "v1",
        "ReplicationController": "v1",
        "ResourceQuota": "v1",
        "LimitRange": "v1",
        "Deployment": "apps/v1",
        "StatefulSet": "apps/v1",
        "DaemonSet": "apps/v1",
        "ReplicaSet": "apps/v1",
        "Ingress": "networking.k8s.io/v1",
        "IngressClass": "networking.k8s.io/v1",
        "NetworkPolicy": "networking.k8s.io/v1",
        "HorizontalPodAutoscaler": "autoscaling/v2",
        "Job": "batch/v1",
        "CronJob": "batch/v1",
        "StorageClass": "storage.k8s.io/v1",
    }.get(kind)


def _resource_for_kind(
    dc: dynamic.DynamicClient,
    kind: str,
    api_version: str | None = None,
):
    """Resolve a Kind to a DynamicClient Resource handle, with CRD support.

    Resolution order:

      1. **Explicit api_version** (caller-supplied, e.g. `"cert-manager.io/v1"`
         for a CRD). Used directly; if the cluster doesn't have it, raises
         a clear `ValueError`. This is the unambiguous path CRDs should use.

      2. **Hardcoded built-in dict** for the ~30 common built-in kinds
         (Pod, Deployment, Service, ...). Fast path that avoids a discovery
         scan on every read.

      3. **DynamicClient auto-discovery** — calls
         `dc.resources.get(kind=kind)` which searches every API group
         registered in the cluster. If exactly one kind matches (the
         normal case for a CRD like `Certificate`), returns it. If none
         matches, raises ValueError. If 2+ groups define a kind with the
         same name (rare; e.g. both `apps/v1` and a CRD operator named
         `Deployment`), raises ValueError pointing the caller at the
         matching api_versions — the caller passes one explicitly.
    """
    if api_version:
        try:
            return dc.resources.get(api_version=api_version, kind=kind)
        except ResourceNotFoundError as e:
            raise ValueError(
                f"Kind '{kind}' not found in api_version={api_version!r}. "
                "Check that the CRD is installed and the version is correct."
            ) from e

    # Fast path — built-in kinds. Fall through to discovery on miss OR
    # ambiguity (a CRD can shadow a built-in kind name in another group).
    builtin_av = _api_version_for(kind)
    if builtin_av is not None:
        try:
            return dc.resources.get(api_version=builtin_av, kind=kind)
        except (ResourceNotFoundError, ResourceNotUniqueError):
            pass  # fall through to discovery

    # Discovery path — scan all groups for a unique match.
    try:
        return dc.resources.get(kind=kind)
    except ResourceNotUniqueError as e:
        matches = list(dc.resources.search(kind=kind))
        options = sorted({m.group_version for m in matches})
        raise ValueError(
            f"Ambiguous kind '{kind}' — matched in {len(options)} API groups: "
            f"{options}. Pass api_version explicitly (e.g. 'apps/v1')."
        ) from e
    except ResourceNotFoundError as e:
        raise ValueError(
            f"Unknown kind '{kind}'. Use `get_api_resources()` to list the "
            "kinds available in this cluster (it includes installed CRDs)."
        ) from e


# ---------- list / get / get_yaml / describe -----------------------------------


def list_resources(
    kind: str,
    namespace: str | None = None,
    label_selector: str | None = None,
    api_version: str | None = None,
    wide: bool = False,
) -> str:
    """List Kubernetes resources by Kind. CRDs are supported — pass `api_version`
    explicitly (`"cert-manager.io/v1"` etc.) or rely on auto-discovery when
    the kind name is unique across API groups.

    Args:
        kind: e.g. "Pod", "Deployment", "Service", "Ingress", "ConfigMap",
            "Certificate" (cert-manager CRD), "Elasticsearch" (ECK CRD), etc.
        namespace: namespace to list in. None = all namespaces.
            Cluster-scoped kinds (Node, Namespace, PersistentVolume) ignore it.
        label_selector: e.g. "app=nginx,tier=frontend".
        api_version: full apiVersion, e.g. `"apps/v1"` for built-ins or
            `"cert-manager.io/v1"` for CRDs. Omit for built-in kinds —
            auto-resolved via the hardcoded dictionary. Required only when
            the same Kind name exists in multiple API groups (rare).
        wide: when True, append kind-specific extra columns (e.g. Node →
            INTERNAL-IP / ROLES; Service → CLUSTER-IP / PORT(S) / EXTERNAL-IP;
            Deployment → READY). Mirrors `kubectl get -o wide`. Default False
            keeps the compact NAME / NAMESPACE / STATUS / AGE shape for
            backward compatibility. For Pod-specific columns (PHASE /
            RESTARTS / NODE) prefer `list_pods()`.

    Returns a compact NAME / NAMESPACE / STATUS / AGE table; with wide=True,
    adds columns per `_WIDE_COLUMNS`.
    """
    dc = _dyn_client()
    resource = _resource_for_kind(dc, kind, api_version=api_version)

    get_kwargs = {"label_selector": label_selector} if label_selector else {}
    if namespace:
        ret = resource.get(namespace=namespace, **get_kwargs)
    else:
        ret = resource.get(**get_kwargs)
    items = ret.items

    wide_cols = _WIDE_COLUMNS.get(kind, ()) if wide else ()

    rows = []
    for item in items:
        obj = _to_dict(item)
        md = obj.get("metadata", {})
        status = obj.get("status", {}) or {}
        row = {
            "NAME": md.get("name"),
            "NAMESPACE": md.get("namespace", ""),
            "STATUS": _status_for(kind, status),
            "AGE": _age(md.get("creationTimestamp")),
        }
        for col, fn in wide_cols:
            row[col] = fn(obj)
        rows.append(row)

    columns = ["NAME", "NAMESPACE", "STATUS", "AGE"]
    columns.extend(c for c, _ in wide_cols)
    return short_table(rows, columns)


def get_resource(
    kind: str,
    name: str,
    namespace: str | None = None,
    api_version: str | None = None,
) -> dict:
    """Fetch a resource as a JSON-serializable dict (apiVersion/kind/metadata/spec/status).

    Supports CRDs — pass `api_version` explicitly or rely on auto-discovery.
    """
    return _fetch(kind, name, namespace, api_version=api_version)


def get_resource_yaml(
    kind: str,
    name: str,
    namespace: str | None = None,
    reveal_secrets: bool = False,
    api_version: str | None = None,
    include_managed_fields: bool = False,
) -> str:
    """Fetch a resource as a YAML manifest. Supports CRDs.

    By default, server-managed metadata fields are stripped from the output:
    `managedFields`, `resourceVersion`, `uid`, `generation`, `selfLink`.
    These are apiserver-stamped bookkeeping that an LLM never edits and that
    `managedFields` alone can take 80% of a Pod manifest. Set
    `include_managed_fields=True` to keep them.

    Args:
        kind, name, namespace: resource identity.
        reveal_secrets: by default, Secret data and stringData values are masked
            with '***'. Set to True to print actual values; the caller is
            responsible for confirming with the user first.
        api_version: optional, e.g. `"cert-manager.io/v1"` for a CRD.
        include_managed_fields: default False — strip server-managed metadata
            fields. Set True to keep them.

    Returns YAML text.
    """
    obj = _fetch(kind, name, namespace, api_version=api_version)
    if kind.lower() == "secret" and not reveal_secrets:
        obj = mask_secret_data(obj)
    obj = _strip_managed_metadata(obj, include_managed_fields=include_managed_fields)
    return to_yaml(obj)


# Server-managed metadata keys: stamped by the apiserver on every resource,
# never user-edited, and `managedFields` in particular is the loudest noise
# in any kubectl-style YAML dump of a real cluster.
_MANAGED_METADATA_KEYS = (
    "managedFields",
    "resourceVersion",
    "uid",
    "generation",
    "selfLink",
)


def _strip_managed_metadata(obj: dict, *, include_managed_fields: bool) -> dict:
    if include_managed_fields or not isinstance(obj, dict):
        return obj
    md = obj.get("metadata")
    if not isinstance(md, dict):
        return obj
    if not any(k in md for k in _MANAGED_METADATA_KEYS):
        return obj  # nothing to strip — avoid a needless copy
    out = dict(obj)
    out["metadata"] = {k: v for k, v in md.items() if k not in _MANAGED_METADATA_KEYS}
    return out


def describe_resource(
    kind: str,
    name: str,
    namespace: str | None = None,
    api_version: str | None = None,
) -> str:
    """Return a kubectl-describe-style text summary. Supports CRDs.

    Note: prefer reusing the most recent result for the same query rather
    than re-calling if the underlying state is unlikely to have changed. New
    calls remain valid when verifying a mutation's effect.
    """
    obj = _fetch(kind, name, namespace, api_version=api_version)
    return describe_fmt(obj)


def replace_resource(yaml_content: str) -> str:
    """⚠️ WRITE — PUT (full replace) a resource with optimistic concurrency.
    The current `resourceVersion` is read first and stamped into the manifest,
    so the apiserver will REFUSE the update with a conflict error if anyone
    else has modified the resource since we read it.

    Use this when multiple agents / humans edit the same resource concurrently
    and you want a hard "no silent overwrite" guarantee. For the more common
    create-or-patch flow, use `apply_yaml` instead. To preview what would
    change without writing, use `diff_resource` first.

    Raises PermissionError when read-only / allowlist blocks writes.
    Raises RuntimeError when the cluster reports a version conflict.
    """
    settings = get_settings()
    if settings.read_only:
        raise PermissionError(
            "Server is in read-only mode (K8S_MCP_READ_ONLY=true). replace is disabled."
        )

    docs = [d for d in yaml.safe_load_all(yaml_content) if d is not None]
    if not docs:
        return "(empty manifest)"

    for doc in docs:
        ns = (doc.get("metadata") or {}).get("namespace")
        if not settings.ns_allowed(ns):
            raise PermissionError(
                f"Write to namespace '{ns}' is not allowed by K8S_MCP_NAMESPACE_ALLOWLIST"
            )

    dc = _dyn_client()
    results = []
    for doc in docs:
        kind = doc.get("kind")
        ns = (doc.get("metadata") or {}).get("namespace")
        name = (doc.get("metadata") or {}).get("name")
        api_version = doc.get("apiVersion")
        try:
            resource = dc.resources.get(api_version=api_version, kind=kind)
        except ResourceNotFoundError as e:
            raise ValueError(f"Unknown kind: {kind}") from e

        # PUT requires the current resourceVersion to enforce optimistic
        # concurrency; fetch it first.
        get_kwargs = {"name": name}
        if ns:
            get_kwargs["namespace"] = ns
        try:
            current = resource.get(**get_kwargs)
            current_dict = _to_dict(current)
        except dynamic.exceptions.NotFoundError as e:
            suffix = f" in namespace '{ns}'" if ns else ""
            raise LookupError(f"{kind} '{name}' not found{suffix}") from e

        doc.setdefault("metadata", {})
        doc["metadata"]["resourceVersion"] = (
            current_dict.get("metadata", {}).get("resourceVersion")
        )

        replace_kwargs = {"body": doc}
        if ns:
            replace_kwargs["namespace"] = ns

        try:
            resource.replace(**replace_kwargs)
            results.append(f"{kind}/{name}: replaced (resourceVersion={doc['metadata']['resourceVersion']})")
        except dynamic.exceptions.ConflictError as e:
            raise RuntimeError(
                f"{kind}/{name}: ResourceVersion conflict — someone else modified it. "
                f"Re-fetch with get_resource_yaml and try again. ({e})"
            ) from e
        except dynamic.exceptions.NotFoundError as e:
            raise LookupError(f"{kind}/{name}: not found ({e})") from e

    return "\n".join(results)


def diff_resource(yaml_content: str) -> str:
    """Read-only preview of what `apply_yaml` would change, without writing
    anything to the cluster. For each manifest:
      - If the resource doesn't exist → "CREATE"
      - If it exists → show added/removed/changed top-level paths

    Use this BEFORE `apply_yaml` to confirm what will change. Multi-doc YAML
    is supported. To actually apply the diff, call `apply_yaml`; if you need
    optimistic concurrency (refuse on concurrent edits), call
    `replace_resource` instead.
    """
    docs = [d for d in yaml.safe_load_all(yaml_content) if d is not None]
    if not docs:
        return "(empty manifest)"

    dc = _dyn_client()
    sections: list[str] = []
    for doc in docs:
        kind = doc.get("kind")
        ns = (doc.get("metadata") or {}).get("namespace")
        name = (doc.get("metadata") or {}).get("name")
        api_version = doc.get("apiVersion")
        try:
            resource = dc.resources.get(api_version=api_version, kind=kind)
        except ResourceNotFoundError as e:
            raise ValueError(f"Unknown kind: {kind}") from e

        get_kwargs = {"name": name}
        if ns:
            get_kwargs["namespace"] = ns

        try:
            current = resource.get(**get_kwargs)
            current_dict = _to_dict(current)
        except (dynamic.exceptions.NotFoundError, ApiException) as e:
            if isinstance(e, ApiException) and e.status != 404:
                raise
            sections.append(f"--- {kind}/{ns}/{name} --- ACTION: CREATE (resource does not exist)")
            continue

        new_dict = doc
        diff_lines = _structural_diff(current_dict, new_dict)
        action = "no changes" if not diff_lines else "UPDATE"
        sections.append(
            f"--- {kind}/{ns}/{name} (resourceVersion={current_dict.get('metadata', {}).get('resourceVersion')}) --- "
            f"ACTION: {action}"
        )
        if diff_lines:
            sections.extend(diff_lines)
    return "\n".join(sections)


def _structural_diff(old: dict, new: dict, prefix: str = "") -> list[str]:
    """Compute top-level field-level differences between two resource dicts.

    Ignores server-managed fields (metadata.resourceVersion, .uid, .managedFields,
    .creationTimestamp, .generation, status) so the diff stays meaningful.
    """
    server_managed_metadata = {
        "resourceVersion", "uid", "managedFields",
        "creationTimestamp", "generation", "selfLink",
    }
    server_managed_top_level = {"status"}

    def strip(d):
        if isinstance(d, dict):
            md = d.get("metadata")
            if isinstance(md, dict):
                for k in server_managed_metadata:
                    md.pop(k, None)
        return d

    old = strip(old)
    new = strip(new)

    lines: list[str] = []
    all_keys = sorted(set(old.keys()) | set(new.keys()))
    for k in all_keys:
        if k in server_managed_top_level:
            continue
        oppath = (old.get(k), new.get(k))
        if oppath[0] == oppath[1]:
            continue
        if oppath[0] is None:
            lines.append(f"  + {k}: {oppath[1]}")
        elif oppath[1] is None:
            lines.append(f"  - {k}: {oppath[0]}")
        else:
            lines.append(f"  ~ {k}: {oppath[0]} -> {oppath[1]}")
    return lines


# ---------- apply --------------------------------------------------------------


def apply_yaml(yaml_content: str) -> str:
    """Apply a YAML manifest (single doc or multi-doc `---` separated).

    Implements kubectl-style client-side apply:
      - If the resource does not exist → create it
      - If it exists → patch it with the supplied body

    For PREVIEW without writing, use `diff_resource` first. For optimistic-
    concurrency PUT (refuse on concurrent edits), use `replace_resource`
    instead. Server-side apply (`?fieldManager=...`) is NOT used; for our
    LLM-agent use-case the create-or-patch flow is sufficient and more
    predictable.

    Safety:
      - Refused when settings.read_only is True.
      - Refused when settings.namespace_allowlist is set and the manifest's
        namespace is not in the allowlist.

    Best practice: ALWAYS call `diff_resource` (or `get_resource_yaml` to
    inspect current state) before applying, and confirm with the user.
    """
    settings = get_settings()
    if settings.read_only:
        raise PermissionError(
            "Server is in read-only mode (K8S_MCP_READ_ONLY=true). apply is disabled."
        )

    docs = [d for d in yaml.safe_load_all(yaml_content) if d is not None]
    if not docs:
        return "(empty manifest)"

    # Validate namespace allowlist BEFORE touching the cluster client.
    for doc in docs:
        ns = (doc.get("metadata") or {}).get("namespace")
        if not settings.ns_allowed(ns):
            raise PermissionError(
                f"Write to namespace '{ns}' is not allowed by K8S_MCP_NAMESPACE_ALLOWLIST"
            )

    dc = _dyn_client()
    results = []
    for doc in docs:
        kind = doc.get("kind")
        ns = (doc.get("metadata") or {}).get("namespace")
        name = (doc.get("metadata") or {}).get("name")
        try:
            resource = dc.resources.get(api_version=doc.get("apiVersion"), kind=kind)
        except ResourceNotFoundError as e:
            raise ValueError(f"Unknown kind: {kind}") from e

        # Try create first; on conflict (already exists), patch.
        try:
            get_kwargs = {"name": name}
            if ns:
                get_kwargs["namespace"] = ns
            try:
                resource.get(**get_kwargs)
                exists = True
            except dynamic.exceptions.NotFoundError:
                exists = False

            if exists:
                # Use strategic merge patch via patch (resource.patch is the dynamic helper)
                create_kwargs = {"body": doc}
                if ns:
                    create_kwargs["namespace"] = ns
                resource.patch(**create_kwargs)
                results.append(f"{kind}/{name}: configured (patched)")
            else:
                create_kwargs = {"body": doc}
                if ns:
                    create_kwargs["namespace"] = ns
                resource.create(**create_kwargs)
                results.append(f"{kind}/{name}: created")
        except dynamic.exceptions.ConflictError as e:
            raise RuntimeError(f"{kind}/{name}: conflict — {e}") from e

    return "\n".join(results)


# ---------- helpers ------------------------------------------------------------


def _fetch(
    kind: str,
    name: str,
    namespace: str | None,
    api_version: str | None = None,
) -> dict:
    dc = _dyn_client()
    resource = _resource_for_kind(dc, kind, api_version=api_version)

    try:
        item = resource.get(name=name, namespace=namespace) if namespace else resource.get(name=name)
    except dynamic.exceptions.NotFoundError as e:
        suffix = f" in namespace '{namespace}'" if namespace else ""
        raise LookupError(f"{kind} '{name}' not found{suffix}") from e

    obj = _to_dict(item)
    if obj.get("metadata", {}).get("name") != name:
        suffix = f" in namespace '{namespace}'" if namespace else ""
        raise LookupError(f"{kind} '{name}' not found{suffix}")
    return obj


# ---------- wide-column extractors ------------------------------------------
#
# These power `list_resources(..., wide=True)`. Each extractor receives the
# resource dict (as returned by DynamicClient's `.to_dict()`) and returns
# the cell value as a string — empty string when the field is absent, so the
# column aligns cleanly in `short_table` even when a row lacks that data.


def _extract_internal_ip(obj: dict) -> str:
    for a in obj.get("status", {}).get("addresses") or []:
        if a.get("type") == "InternalIP":
            return a.get("address", "") or ""
    return ""


def _extract_node_roles(obj: dict) -> str:
    """Comma-separated role names (stripped of the `node-role.kubernetes.io/` prefix)."""
    labels = obj.get("metadata", {}).get("labels") or {}
    roles = sorted(
        k.split("/", 1)[1]
        for k in labels
        if k.startswith("node-role.kubernetes.io/")
    )
    return ",".join(roles)


def _extract_pod_restarts(obj: dict) -> str:
    cs = obj.get("status", {}).get("containerStatuses") or []
    return str(sum(c.get("restartCount", 0) for c in cs))


def _extract_pod_node(obj: dict) -> str:
    return obj.get("spec", {}).get("nodeName", "") or ""


def _extract_pod_ip(obj: dict) -> str:
    return obj.get("status", {}).get("podIP", "") or ""


def _extract_service_cluster_ip(obj: dict) -> str:
    return obj.get("spec", {}).get("clusterIP", "") or ""


def _extract_service_ports(obj: dict) -> str:
    parts: list[str] = []
    for p in obj.get("spec", {}).get("ports") or []:
        port = p.get("port")
        target = p.get("targetPort")
        proto = p.get("protocol", "TCP")
        if target is not None and target != port:
            parts.append(f"{port}:{target}/{proto}")
        else:
            parts.append(f"{port}/{proto}")
    return ",".join(parts)


def _extract_service_external_ip(obj: dict) -> str:
    ingress = obj.get("status", {}).get("loadBalancer", {}).get("ingress") or []
    if not ingress:
        return ""
    return ",".join(i.get("ip") or i.get("hostname", "") or "" for i in ingress)


def _extract_workload_ready(obj: dict) -> str:
    s = obj.get("status") or {}
    ready = s.get("readyReplicas")
    desired = s.get("replicas")
    if ready is None and desired is None:
        return ""
    return f"{ready or 0}/{desired or '?'}"


_WIDE_COLUMNS: dict[str, tuple[tuple[str, Callable[[dict], str]], ...]] = {
    "Node": (
        ("INTERNAL-IP", _extract_internal_ip),
        ("ROLES", _extract_node_roles),
    ),
    "Pod": (
        ("RESTARTS", _extract_pod_restarts),
        ("NODE", _extract_pod_node),
        ("IP", _extract_pod_ip),
    ),
    "Service": (
        ("CLUSTER-IP", _extract_service_cluster_ip),
        ("PORT(S)", _extract_service_ports),
        ("EXTERNAL-IP", _extract_service_external_ip),
    ),
    "Deployment": (("READY", _extract_workload_ready),),
    "StatefulSet": (("READY", _extract_workload_ready),),
    "DaemonSet": (("READY", _extract_workload_ready),),
    "ReplicaSet": (("READY", _extract_workload_ready),),
}


def _status_for(kind: str, status: dict) -> str:
    if not status:
        return ""
    if "phase" in status:
        return status["phase"]
    if kind in ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"):
        ready = status.get("readyReplicas")
        desired = status.get("replicas")
        if ready is not None:
            return f"{ready}/{desired or '?'} Ready"
    if kind == "Service":
        lb = status.get("loadBalancer") or {}
        ingress = lb.get("ingress") or []
        if ingress:
            return " ".join(i.get("hostname") or i.get("ip", "") for i in ingress)
    if kind == "Pod":
        return status.get("phase", "")
    return ""


def _age(created: str | None) -> str:
    if not created:
        return ""
    from datetime import datetime
    try:
        ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
        delta = datetime.now(UTC) - ts
    except (ValueError, TypeError):
        return created
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def register(mcp) -> None:
    """Register all tools in this module with the FastMCP instance."""
    mcp.tool()(list_resources)
    mcp.tool()(get_resource)
    mcp.tool()(get_resource_yaml)
    mcp.tool()(describe_resource)
    mcp.tool()(apply_yaml)
    mcp.tool()(replace_resource)
    mcp.tool()(diff_resource)
