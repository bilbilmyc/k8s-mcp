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
from typing import Any

import yaml
from kubernetes import dynamic
from kubernetes.client.rest import ApiException
from kubernetes.dynamic.exceptions import ResourceNotFoundError, ResourceNotUniqueError

from ..client import get_api_client
from ..config import get_settings
from ..formatters import describe as describe_fmt
from ..formatters import format_age, mask_secret_data, short_table, to_yaml

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
    field_selector: str | None = None,
    limit: int | None = None,
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
        field_selector: server-side field selector, e.g.
            `"status.phase=Running"` or `"metadata.namespace!=kube-system"`.
            Pushed to the apiserver, so it bounds the wire size and avoids
            a client-side filter over a 50k-row response.
        limit: server-side cap on items returned. None = apiserver default
            (typically 500 for list, but depends on the resource). Use a
            smaller number for chatty kinds, or pair with `field_selector`
            to push filtering to the apiserver.
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
    adds columns per `_WIDE_COLUMNS`. When the apiserver-side truncation
    is detectable (response length == limit), a footer hint suggests
    narrowing the query — but most kinds don't expose the total count
    cheaply, so the footer only fires when we know it.
    """
    rows = _list_resource_rows(
        kind,
        namespace=namespace,
        label_selector=label_selector,
        field_selector=field_selector,
        limit=limit,
        api_version=api_version,
        wide=wide,
    )

    wide_cols = _WIDE_COLUMNS.get(kind, ()) if wide else ()
    columns = ["NAME", "NAMESPACE", "STATUS", "AGE"]
    columns.extend(c for c, _ in wide_cols)
    table = short_table(rows, columns)
    # When a limit was requested and the response came back full, surface
    # a hint so the agent / operator knows to narrow the query. Without
    # this, a "kubectl get cm --all-namespaces" that silently returned
    # 500/500 looks identical to "no more ConfigMaps".
    if limit is not None and len(rows) >= int(limit):
        table += (
            f"\n(showing first {int(limit)} items — raise `limit=` or "
            f"add `field_selector=` / `label_selector=` to see more)"
        )
    return table


def _list_resource_rows(
    kind: str,
    namespace: str | None = None,
    label_selector: str | None = None,
    field_selector: str | None = None,
    limit: int | None = None,
    api_version: str | None = None,
    wide: bool = False,
) -> list[dict]:
    """Lower-level helper: fetch a kind and return row dicts.

    Same wire behavior as `list_resources` but returns the rows as
    plain dicts so callers (e.g. `search_resources`) can aggregate
    across multiple kinds without re-parsing rendered text.
    """
    dc = _dyn_client()
    resource = _resource_for_kind(dc, kind, api_version=api_version)

    get_kwargs: dict = {}
    if label_selector:
        get_kwargs["label_selector"] = label_selector
    if field_selector:
        get_kwargs["field_selector"] = field_selector
    if limit is not None:
        get_kwargs["limit"] = int(limit)
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
            "AGE": format_age(md.get("creationTimestamp")),
        }
        for col, fn in wide_cols:
            row[col] = fn(obj)
        rows.append(row)
    return rows


# Default kinds searched by `search_resources`. Mirrors the hardcoded
# dict in `_api_version_for()` — kept as a tuple so callers can iterate
# without depending on dict-iteration order. CRDs are searchable by
# passing `kinds=[...]` explicitly (they need an api_version, which is
# expensive to auto-discover for every call).
_SEARCHABLE_BUILTIN_KINDS: tuple[str, ...] = (
    "Pod", "Service", "ConfigMap", "Secret", "Namespace", "Node",
    "PersistentVolume", "PersistentVolumeClaim", "ServiceAccount",
    "Endpoints", "Event", "ReplicationController", "ResourceQuota",
    "LimitRange", "Deployment", "StatefulSet", "DaemonSet", "ReplicaSet",
    "Ingress", "IngressClass", "NetworkPolicy", "HorizontalPodAutoscaler",
    "Job", "CronJob", "StorageClass",
)


def search_resources(
    name_substring: str,
    namespace: str | None = None,
    kinds: list[str] | None = None,
    label_selector: str | None = None,
    limit_per_kind: int = 50,
    api_versions: dict[str, str] | None = None,
) -> str:
    """Find resources by name substring across multiple kinds / namespaces.

    Solves the "I forgot what kind or namespace X is in" triage problem
    — calling `list_resources(kind=K)` once per candidate K is wasteful
    when you don't know which one matches.

    Args:
        name_substring: case-insensitive substring to match against
            `metadata.name`. Required and must be non-empty.
        namespace: namespace to search in; `None` = all namespaces.
            Cluster-scoped kinds (Node, PersistentVolume, ...) ignore it.
        kinds: explicit list of Kinds to search. `None` (default) =
            all built-in kinds (~25: Pod, Deployment, Service, ...).
            Pass `kinds=["Certificate", "Ingress"]` to include CRDs;
            pair with `api_versions=` to give each CRD its api_version.
            Auto-discovering every CRD on every call would be too
            expensive on clusters with 100+ CRDs.
        label_selector: e.g. `"app=nginx,tier=frontend"`.
        limit_per_kind: per-kind server-side cap. Default 50 keeps the
            total response bounded. Raise it for thorough sweeps.
        api_versions: optional mapping of `kind → apiVersion` for CRDs
            in `kinds`. E.g. `{"Certificate": "cert-manager.io/v1"}`.
            Without this, CRDs in `kinds` will fail to resolve.

    Returns a per-row `KIND / NAME / NAMESPACE / STATUS / AGE` table,
    sorted by KIND then NAME. Kinds that fail (RBAC forbidden, CRD not
    installed) are skipped and the count surfaces in the footer.
    """
    if not name_substring or not name_substring.strip():
        raise ValueError("name_substring must be a non-empty string")

    needle = name_substring.strip().lower()
    if kinds is None:
        search_kinds = list(_SEARCHABLE_BUILTIN_KINDS)
    else:
        search_kinds = list(kinds)
    if not search_kinds:
        raise ValueError("kinds must be a non-empty list")

    api_versions = api_versions or {}
    skipped: list[tuple[str, str]] = []

    def _worker(kind: str) -> list[dict]:
        try:
            rows = _list_resource_rows(
                kind,
                namespace=namespace,
                label_selector=label_selector,
                limit=limit_per_kind,
                api_version=api_versions.get(kind),
            )
        except ApiException as e:
            skipped.append((kind, f"api error {e.status} {e.reason}"))
            return []
        except ValueError as e:
            # CRD not installed / unknown kind / ambiguous match — skip
            # but surface the reason in the footer so the caller knows.
            skipped.append((kind, str(e).split("\n", 1)[0][:80]))
            return []
        for r in rows:
            r["KIND"] = kind
        return rows

    all_rows: list[dict] = []
    # Same threshold pattern as get_pod_logs multi-pod fan-out: small
    # queries stay serial (avoids thread-pool overhead + keeps call
    # order stable for tests), ≥5 kinds parallelize on a pool of 8.
    if len(search_kinds) >= 5:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(
            max_workers=min(8, len(search_kinds)),
        ) as ex:
            for rows in ex.map(_worker, search_kinds):
                all_rows.extend(rows)
    else:
        for kind in search_kinds:
            all_rows.extend(_worker(kind))

    matched = [r for r in all_rows if needle in (r.get("NAME") or "").lower()]
    matched.sort(key=lambda r: (r["KIND"], r.get("NAME") or ""))

    columns = ["KIND", "NAME", "NAMESPACE", "STATUS", "AGE"]
    table = short_table(matched, columns)

    footer_lines: list[str] = []
    if not matched:
        if skipped:
            kinds_tried = ", ".join(k for k, _ in skipped[:5])
            footer_lines.append(
                f"(no resources named like '{name_substring}' — tried "
                f"{len(skipped)} kind(s) that errored: {kinds_tried}"
                f"{', ...' if len(skipped) > 5 else ''})"
            )
        else:
            footer_lines.append(
                f"(no resources named like '{name_substring}')"
            )
    elif skipped:
        footer_lines.append(
            f"(searched {len(search_kinds)} kinds; "
            f"{len(skipped)} skipped: "
            + ", ".join(f"{k}={reason}" for k, reason in skipped[:3])
            + ("..." if len(skipped) > 3 else "")
            + ")"
        )
    if footer_lines:
        table += "\n" + "\n".join(footer_lines)
    return table


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
# never user-edited. The full set is used by `_structural_diff` (to ignore
# fields that change on every read); a subset of these are pure noise we
# hide in YAML output (`_strip_managed_metadata`). `managedFields` in
# particular is the loudest noise in any kubectl-style YAML dump of a
# real cluster.
_SERVER_MANAGED_METADATA_KEYS: frozenset[str] = frozenset({
    "managedFields",
    "resourceVersion",
    "uid",
    "generation",
    "selfLink",
    "creationTimestamp",
})

# Fields we actively hide in YAML output.creationTimestamp is intentionally
# NOT in this set — it's useful to humans ("when was this created?"), even
# though the apiserver stamps it.
_YAML_NOISE_METADATA_KEYS: frozenset[str] = frozenset({
    "managedFields",
    "resourceVersion",
    "uid",
    "generation",
    "selfLink",
})


def _strip_managed_metadata(obj: dict, *, include_managed_fields: bool) -> dict:
    if include_managed_fields or not isinstance(obj, dict):
        return obj
    md = obj.get("metadata")
    if not isinstance(md, dict):
        return obj
    if not any(k in md for k in _YAML_NOISE_METADATA_KEYS):
        return obj  # nothing to strip — avoid a needless copy
    out = dict(obj)
    out["metadata"] = {k: v for k, v in md.items() if k not in _YAML_NOISE_METADATA_KEYS}
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


# ---------- atomic label patches ---------------------------------------------


def _jsonpatch_escape(token: str) -> str:
    """Escape a label key for use as a JSON Pointer reference token (RFC 6901).

    K8s label keys commonly contain `.` (e.g. `app.kubernetes.io/name`) which
    is fine, but `/` and `~` need escaping because the apiserver's JSON Patch
    handler parses paths as `/metadata/labels/<escaped-token>`.
    """
    return token.replace("~", "~0").replace("/", "~1")


def add_label(
    kind: str,
    name: str,
    key: str,
    value: str,
    namespace: str | None = None,
    api_version: str | None = None,
) -> str:
    """⚠️ WRITE — atomically add or update a single label on a resource.

    Uses a JSON Patch `add` to touch only the targeted label, leaving
    every other field (status, managedFields, other labels, annotations)
    untouched — the safer alternative to `replace_resource` when the goal
    is just one label change. The patch fails if the resource doesn't
    exist or RBAC denies writes; the resource is unchanged either way.

    Args:
        kind, name, namespace, api_version: standard resource locator.
        key: label key (e.g. `"app.kubernetes.io/name"`). Validated by
            the apiserver (max 63 chars per part, `[a-z0-9A-Z][-a-z0-9A-Z.]*`).
        value: label value (e.g. `"web"`).

    Returns a one-line confirmation. Errors propagate.

    Raises PermissionError when read-only / allowlist blocks writes.
    """
    if not key or not isinstance(key, str):
        raise ValueError("label key must be a non-empty string")
    if value is None or not isinstance(value, str):
        raise ValueError("label value must be a string")

    settings = get_settings()
    if settings.read_only:
        raise PermissionError(
            "Server is in read-only mode (K8S_MCP_READ_ONLY=true). label is disabled."
        )
    if not settings.ns_allowed(namespace):
        raise PermissionError(
            f"Write to namespace '{namespace}' is not allowed by K8S_MCP_NAMESPACE_ALLOWLIST"
        )

    dc = _dyn_client()
    resource = _resource_for_kind(dc, kind, api_version=api_version)
    patch_body = [{
        "op": "add",
        "path": f"/metadata/labels/{_jsonpatch_escape(key)}",
        "value": value,
    }]

    try:
        patch_kwargs: dict = {
            "name": name,
            "body": patch_body,
            "content_type": "application/json-patch+json",
        }
        if namespace:
            patch_kwargs["namespace"] = namespace
        resource.patch(**patch_kwargs)
    except ApiException as e:
        ns_prefix = f"{namespace}/" if namespace else ""
        raise RuntimeError(
            f"failed to add label {key}={value} to {kind}/{ns_prefix}{name}: "
            f"{e.status} {e.reason}"
        ) from e

    ns_prefix = f"{namespace}/" if namespace else ""
    return f"✅ added label {key}={value} to {kind}/{ns_prefix}{name}"


def remove_label(
    kind: str,
    name: str,
    key: str,
    namespace: str | None = None,
    api_version: str | None = None,
) -> str:
    """⚠️ WRITE — atomically remove a single label from a resource.

    Uses a strategic-merge patch with `null` value — K8s interprets this
    as "remove this key from the labels map". Idempotent: if the label
    isn't on the resource (or the labels map itself is missing), the
    patch is a no-op and the call returns a friendly warning.

    Args: same as `add_label` minus `value`.

    Returns a one-line confirmation. Errors other than "label not found"
    propagate.

    Raises PermissionError when read-only / allowlist blocks writes.
    """
    if not key or not isinstance(key, str):
        raise ValueError("label key must be a non-empty string")

    settings = get_settings()
    if settings.read_only:
        raise PermissionError(
            "Server is in read-only mode (K8S_MCP_READ_ONLY=true). label is disabled."
        )
    if not settings.ns_allowed(namespace):
        raise PermissionError(
            f"Write to namespace '{namespace}' is not allowed by K8S_MCP_NAMESPACE_ALLOWLIST"
        )

    dc = _dyn_client()
    resource = _resource_for_kind(dc, kind, api_version=api_version)
    # Strategic merge patch: {"metadata":{"labels":{"foo":null}}} removes
    # the `foo` key. If the labels map doesn't have `foo` (or doesn't
    # exist at all), the apiserver treats it as a no-op — same as
    # `kubectl label foo bar-`.
    patch_body = {"metadata": {"labels": {key: None}}}

    try:
        patch_kwargs: dict = {"name": name, "body": patch_body}
        if namespace:
            patch_kwargs["namespace"] = namespace
        resource.patch(**patch_kwargs)
    except ApiException as e:
        # 404 = the resource itself doesn't exist (real error).
        # Anything else propagates.
        ns_prefix = f"{namespace}/" if namespace else ""
        raise RuntimeError(
            f"failed to remove label {key} from {kind}/{ns_prefix}{name}: "
            f"{e.status} {e.reason}"
        ) from e

    ns_prefix = f"{namespace}/" if namespace else ""
    return f"✅ removed label {key} from {kind}/{ns_prefix}{name}"


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
    server_managed_top_level = {"status"}

    def strip(d):
        if isinstance(d, dict):
            md = d.get("metadata")
            if isinstance(md, dict):
                for k in _SERVER_MANAGED_METADATA_KEYS:
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

    Returns one line per applied resource in the form
    "{kind}/{namespace}/{name}: <action>" where `<action>` is one of
    `created` / `configured (patched)` / `unchanged`. Internal callers
    that need the structured per-doc result should use `_apply_yaml_records`.
    """
    records = _apply_yaml_records(yaml_content)
    if records == ["(empty manifest)"]:
        return "(empty manifest)"
    # Match the legacy "kind/name: action" shape (no namespace prefix) for
    # backward compat with downstream callers / tests. The structured form
    # is available via `_apply_yaml_records`.
    lines = [
        f"{r['kind']}/{r['name']}: {r['action']}"
        for r in records
    ]
    return "\n".join(lines)


def _apply_yaml_records(yaml_content: str) -> list[dict] | list[str]:
    """Structured version of `apply_yaml`.

    Returns either `["(empty manifest)"]` for the empty-doc sentinel, or a
    list of per-doc records:
        {
            "kind": str,
            "name": str,
            "namespace": str | None,
            "action": "created" | "configured (patched)" | "unchanged",
            "error": str | None,
        }

    Raises PermissionError for read_only / allowlist violations,
    ValueError for unknown kinds, RuntimeError for resource conflicts.
    """
    settings = get_settings()
    if settings.read_only:
        raise PermissionError(
            "Server is in read-only mode (K8S_MCP_READ_ONLY=true). apply is disabled."
        )

    docs = [d for d in yaml.safe_load_all(yaml_content) if d is not None]
    if not docs:
        return ["(empty manifest)"]

    # Validate namespace allowlist BEFORE touching the cluster client.
    for doc in docs:
        ns = (doc.get("metadata") or {}).get("namespace")
        if not settings.ns_allowed(ns):
            raise PermissionError(
                f"Write to namespace '{ns}' is not allowed by K8S_MCP_NAMESPACE_ALLOWLIST"
            )

    dc = _dyn_client()
    records: list[dict] = []
    for doc in docs:
        kind = doc.get("kind")
        ns = (doc.get("metadata") or {}).get("namespace")
        name = (doc.get("metadata") or {}).get("name")
        try:
            resource = dc.resources.get(api_version=doc.get("apiVersion"), kind=kind)
        except ResourceNotFoundError as e:
            raise ValueError(f"Unknown kind: {kind}") from e

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
                patch_kwargs = {"body": doc}
                if ns:
                    patch_kwargs["namespace"] = ns
                resource.patch(**patch_kwargs)
                records.append({
                    "kind": kind, "name": name, "namespace": ns,
                    "action": "configured (patched)", "error": None,
                })
            else:
                create_kwargs = {"body": doc}
                if ns:
                    create_kwargs["namespace"] = ns
                resource.create(**create_kwargs)
                records.append({
                    "kind": kind, "name": name, "namespace": ns,
                    "action": "created", "error": None,
                })
        except dynamic.exceptions.ConflictError as e:
            raise RuntimeError(f"{kind}/{name}: conflict — {e}") from e

    return records


def _patch_resource_no_check(
    api_version: str,
    kind: str,
    name: str,
    namespace: str | None,
    body: dict,
) -> None:
    """PATCH a resource directly, skipping the existence read.

    Used by `bulk._execute_patches` after it has already listed the
    matched set — saves one extra `resource.get()` per workload. Same
    allowlist / read_only gate as `_apply_yaml_records` (callers must
    check, since this helper trusts the caller about write-readiness).
    """
    dc = _dyn_client()
    resource = dc.resources.get(api_version=api_version, kind=kind)
    patch_kwargs: dict = {"body": body}
    if namespace:
        patch_kwargs["namespace"] = namespace
    resource.patch(**patch_kwargs)


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


def register(mcp) -> None:
    """Register all tools in this module with the FastMCP instance."""
    mcp.tool()(list_resources)
    mcp.tool()(search_resources)
    mcp.tool()(get_resource)
    mcp.tool()(get_resource_yaml)
    mcp.tool()(describe_resource)
    mcp.tool()(add_label)
    mcp.tool()(remove_label)
    mcp.tool()(apply_yaml)
    mcp.tool()(replace_resource)
    mcp.tool()(diff_resource)
