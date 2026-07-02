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

    Returns a compact NAME / NAMESPACE / STATUS / AGE table.
    """
    dc = _dyn_client()
    resource = _resource_for_kind(dc, kind, api_version=api_version)

    get_kwargs = {"label_selector": label_selector} if label_selector else {}
    if namespace:
        ret = resource.get(namespace=namespace, **get_kwargs)
    else:
        ret = resource.get(**get_kwargs)
    items = ret.items

    rows = []
    for item in items:
        obj = _to_dict(item)
        md = obj.get("metadata", {})
        status = obj.get("status", {}) or {}
        rows.append({
            "NAME": md.get("name"),
            "NAMESPACE": md.get("namespace", ""),
            "STATUS": _status_for(kind, status),
            "AGE": _age(md.get("creationTimestamp")),
        })

    return short_table(rows, ["NAME", "NAMESPACE", "STATUS", "AGE"])


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
) -> str:
    """Fetch a resource as a YAML manifest. Supports CRDs.

    Args:
        kind, name, namespace: resource identity.
        reveal_secrets: by default, Secret data and stringData values are masked
            with '***'. Set to True to print actual values; the caller is
            responsible for confirming with the user first.
        api_version: optional, e.g. `"cert-manager.io/v1"` for a CRD.

    Returns YAML text.
    """
    obj = _fetch(kind, name, namespace, api_version=api_version)
    if kind.lower() == "secret" and not reveal_secrets:
        obj = mask_secret_data(obj)
    return to_yaml(obj)


def describe_resource(
    kind: str,
    name: str,
    namespace: str | None = None,
    api_version: str | None = None,
) -> str:
    """Return a kubectl-describe-style text summary. Supports CRDs."""
    obj = _fetch(kind, name, namespace, api_version=api_version)
    return describe_fmt(obj)


def replace_resource(yaml_content: str) -> str:
    """Replace a resource (HTTP PUT semantics) — ResourceVersion is enforced.

    Unlike `apply_yaml` (create-or-patch), this tool sends a real PUT with
    the resource's current `resourceVersion` attached, so the apiserver
    will refuse the update if someone else has modified it since we read it.
    This is the right tool when multiple agents / humans edit the same
    resource concurrently.

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
    """Preview the changes `apply_yaml` would make, without applying.

    For each manifest in the YAML:
      - If the resource doesn't exist → "CREATE"
      - If it exists → show added/removed/changed top-level paths

    Use this before apply to confirm what will change. Multi-doc YAML is
    supported.
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
    Server-side apply (`?fieldManager=...`) is NOT used; for our LLM-agent
    use-case the create-or-patch flow is sufficient and more predictable.

    Safety:
      - Refused when settings.read_only is True.
      - Refused when settings.namespace_allowlist is set and the manifest's
        namespace is not in the allowlist.

    Best practice: ALWAYS call get_resource_yaml first to inspect current state
    before applying, and confirm with the user.
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
