"""Cluster discovery / schema introspection: get_api_resources + explain_resource.

These are the "kubectl api-resources / kubectl explain" equivalents. They let
an LLM agent discover what's in the cluster (especially CRDs) and look up
the schema for kinds it doesn't know — without those, an agent is limited to
the built-in kinds hardcoded in our generic tools.

Both are read-only and bypass the namespace allowlist.

中文说明：
发现/自省类工具，让 Agent 在写 YAML 之前能动态了解集群里有什么 kind：

  - `get_api_resources(prefix=...)`：列出所有 API 资源（含 CRD），
    字段与 `kubectl api-resources` 一致。
  - `explain_resource(kind, field_path=..., api_version=...)`：通过
    OpenAPI schema 反查 kind / 字段的定义与描述，等价于
    `kubectl explain`。

两个工具都只读，自动绕开 namespace allowlist（只读不需要守门）。
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.parse
from concurrent.futures import Future

from kubernetes.dynamic.resource import ResourceList

from ..client import client_cache_key, get_api_client, get_dynamic_client
from ..formatters import short_table

logger = logging.getLogger(__name__)


def get_api_resources(prefix: str | None = None) -> str:
    """List all API resources discoverable in the cluster — call THIS first
    before passing kind names to `list_resources` / `get_resource` /
    `get_resource_yaml`. Lets you confirm the exact spelling, apiVersion,
    and whether the kind is namespaced.

    Equivalent to `kubectl api-resources`. Includes CRDs (custom resources
    registered in the cluster), so this is the right way to ask "what
    kinds exist here?". Pass `prefix="deploy"` to narrow the table.

    Args:
        prefix: optional filter, e.g. "deploy" → matches Deployment, etc.

    Returns a NAME / APIVERSION / NAMESPACED / KIND table — same fields as
    `kubectl api-resources`.

    Includes CRDs (custom resources registered in the cluster), so this is
    the right way to ask "what kinds exist here?".
    """
    rows: list[dict[str, str]] = []
    needle = prefix.lower() if prefix else None
    # LazyDiscoverer.__iter__ yields one list per Kind (the SDK stores
    # multiple same-Kind resources together), so flatten those groups.
    try:
        for discovered_group in get_dynamic_client().resources:
            resources = discovered_group if isinstance(discovered_group, list) else [discovered_group]
            for resource in resources:
                if isinstance(resource, ResourceList):
                    continue
                name = (resource.name or "").strip()
                kind = (resource.kind or "").strip()
                if not name or not kind or "/" in name:
                    continue
                if needle and needle not in name.lower() and needle not in kind.lower():
                    continue
                rows.append({
                    "NAME": name,
                    "SHORTNAMES": ",".join(resource.short_names or []),
                    "APIVERSION": resource.group_version,
                    "NAMESPACED": "true" if resource.namespaced else "false",
                    "KIND": kind,
                    "VERBS": ",".join((resource.verbs or [])[:4]),
                })
    except Exception as exc:  # noqa: BLE001 - keep resources from healthy API groups
        logger.debug("api_resources: discovery stopped after a group failure: %s", exc)

    if not rows:
        return f"(no API resources match prefix={prefix!r})"
    rows.sort(key=lambda x: (x["APIVERSION"], x["KIND"]))
    return short_table(rows, ["NAME", "SHORTNAMES", "APIVERSION", "NAMESPACED", "KIND", "VERBS"])

# =============================================================================
# explain_resource — kubectl explain via aggregate OpenAPI schema
# =============================================================================


def explain_resource(
    kind: str,
    field_path: str | None = None,
    api_version: str | None = None,
) -> str:
    """Look up the schema (and description) for a Kind or a nested field —
    call THIS BEFORE writing a YAML manifest for a kind you don't know, so
    you don't miss required fields, get enum values wrong, or nest fields
    at the wrong level.

    Equivalent to `kubectl explain <kind>[.<field_path>]`. For pure kind
    enumeration (does this kind exist in this cluster?), use
    `get_api_resources` instead.

    Args:
        kind: e.g. "Pod", "Deployment", "HorizontalPodAutoscaler".
        field_path: optional dotted path into the resource, e.g.
            "spec.template.spec.containers". When omitted, returns the
            top-level description and a list of top-level fields.
        api_version: optional, e.g. "apps/v1". When omitted, the first
            matching definition in the OpenAPI schema is used.

    Returns a text description; raise LookupError if the kind is not in
    the schema.
    """
    schema = _get_openapi_schema()
    kind_def = _find_kind_def(schema, kind, api_version)
    if not kind_def:
        raise LookupError(f"Kind '{kind}' (api_version={api_version!r}) not in OpenAPI schema")

    if not field_path:
        return _explain_kind(kind, kind_def)

    target = _drill(kind_def, field_path)
    if not target:
        raise LookupError(
            f"Field path '{field_path}' not found on {kind}. "
            f"Top-level fields: {_top_field_names(kind_def)}"
        )
    return _explain_field(kind, field_path, target)


# ---------- internals ----------------------------------------------------------


# OpenAPI schema cache. The schema rarely changes during a session (only on
# CRD install/upgrade) and costs one apiserver round-trip to fetch — once per
# explain_resource call would be wasteful. We TTL the cache for 5 minutes so
# a long-running MCP session that installs a CRD mid-flight sees the new type
# within 5 minutes without paying the fetch cost on every explain_resource.
#
# We also cap at `_OPENAPI_CACHE_MAX_BYTES` (post JSON-serialize). The core
# K8s schema fits comfortably (≈1–2 MiB), but CRD-heavy clusters can push it
# past tens of MiB. A long-lived MCP server pinning 50 MiB of rarely-touched
# schema is a memory bomb waiting to happen. When the freshly-fetched schema
# would exceed the cap, we return it but skip caching — next call refetches.
_openapi_cache: dict | None = None
_openapi_cache_at: float = 0.0
_openapi_cache_key: tuple | None = None
_OPENAPI_CACHE_TTL_SECONDS = 300
_OPENAPI_CACHE_MAX_BYTES = 8 * 1024 * 1024  # 8 MiB
_openapi_cache_lock = threading.Lock()
_openapi_inflight: dict[tuple, Future[dict]] = {}


def _call_openapi_json(relative_url: str) -> dict:
    """Call one authenticated OpenAPI endpoint and return its JSON object."""
    parsed = urllib.parse.urlsplit(relative_url)
    api_client = get_api_client()
    kwargs = {
        "query_params": urllib.parse.parse_qsl(parsed.query),
        "header_params": {"Accept": "application/json"},
        "auth_settings": ["BearerToken"],
        "_return_http_data_only": True,
    }
    try:
        # Kubernetes 30+ renamed the single response type parameter to a
        # status-code map. Keep the declared kubernetes>=29 compatibility.
        result = api_client.call_api(
            parsed.path,
            "GET",
            response_types_map={200: "object"},
            **kwargs,
        )
    except TypeError as exc:
        if "response_types_map" not in str(exc):
            raise
        result = api_client.call_api(
            parsed.path,
            "GET",
            response_type="object",
            **kwargs,
        )
    if isinstance(result, tuple):
        result = result[0]
    return result if isinstance(result, dict) else {}


def _fetch_openapi_spec() -> dict:
    """Fetch one aggregate schema, preferring the single-call v2 endpoint.

    Kubernetes still serves aggregate OpenAPI v2 alongside v3. V2 is one
    request and therefore substantially cheaper than fetching every path in
    the v3 index. If v2 is disabled, fetch and merge the v3 group-version
    documents advertised by `/openapi/v3`.
    """
    try:
        v2 = _call_openapi_json("/openapi/v2")
    except Exception as e:  # noqa: BLE001 — fall back to v3
        logger.debug("OpenAPI v2 fetch failed; trying v3: %s", e)
    else:
        definitions = v2.get("definitions")
        if isinstance(definitions, dict):
            return {"components": {"schemas": definitions}}

    try:
        index = _call_openapi_json("/openapi/v3")
    except Exception as e:  # noqa: BLE001 — surface as an empty schema
        logger.debug("OpenAPI v3 index fetch failed: %s", e)
        return {}

    merged: dict = {}
    for item in (index.get("paths") or {}).values():
        relative_url = item.get("serverRelativeURL") if isinstance(item, dict) else None
        if not relative_url:
            continue
        try:
            document = _call_openapi_json(relative_url)
        except Exception as e:  # noqa: BLE001 — one unavailable API group should not hide all others
            logger.debug("OpenAPI v3 document %s failed: %s", relative_url, e)
            continue
        schemas = document.get("components", {}).get("schemas", {})
        if isinstance(schemas, dict):
            merged.update(schemas)
    return {"components": {"schemas": merged}}


def _fits_openapi_cache(value: dict) -> bool:
    """Measure encoded JSON incrementally and stop once the cap is exceeded."""
    total = 0
    for chunk in json.JSONEncoder().iterencode(value):
        total += len(chunk.encode("utf-8"))
        if total > _OPENAPI_CACHE_MAX_BYTES:
            return False
    return True


def _store_openapi_spec_if_within_cap(
    spec: dict | None,
    *,
    cache_key: tuple | None = None,
) -> dict:
    """Apply the size-cap policy to a freshly-fetched spec.

    Returns the spec to cache (or `None` if it's too big to retain). Split
    out from `_get_openapi_schema` so the cap policy is testable without
    touching the kubernetes client.
    """
    global _openapi_cache, _openapi_cache_at, _openapi_cache_key
    schemas = (
        spec.get("components", {}).get("schemas", {})
        if isinstance(spec, dict)
        else {}
    )
    if _fits_openapi_cache(schemas):
        _openapi_cache = schemas
        _openapi_cache_at = _now()
        _openapi_cache_key = cache_key
        return _openapi_cache
    # Schema too big to cache — leave cache empty so next call refetches.
    _openapi_cache = None
    _openapi_cache_at = 0.0
    _openapi_cache_key = None
    return schemas


def _now() -> float:
    """Inlined so tests can monkeypatch `time.monotonic` without import
    weirdness. Defaults to a real monotonic clock."""
    import time
    return time.monotonic()


def _get_openapi_schema() -> dict:
    """Fetch and cache the cluster's OpenAPI v3 schema (lazy, TTL'd, size-capped)."""
    global _openapi_cache, _openapi_cache_at, _openapi_cache_key
    key = client_cache_key()
    with _openapi_cache_lock:
        if _openapi_cache_key != key:
            _openapi_cache = None
            _openapi_cache_at = 0.0
            _openapi_cache_key = None
        if (
            _openapi_cache is not None
            and (_now() - _openapi_cache_at) <= _OPENAPI_CACHE_TTL_SECONDS
        ):
            return _openapi_cache
        future = _openapi_inflight.get(key)
        leader = future is None
        if leader:
            future = Future()
            _openapi_inflight[key] = future

    if not leader:
        return future.result()

    try:
        spec = _fetch_openapi_spec()
        with _openapi_cache_lock:
            schemas = _store_openapi_spec_if_within_cap(spec, cache_key=key)
        future.set_result(schemas)
        return schemas
    except BaseException as exc:
        future.set_exception(exc)
        # A leader has no consumer for its own Future exception. Retrieving it
        # prevents Python from reporting an unobserved Future during cleanup.
        future.exception()
        raise
    finally:
        with _openapi_cache_lock:
            if _openapi_inflight.get(key) is future:
                _openapi_inflight.pop(key, None)


def reset_openapi_cache() -> None:
    """Clear the OpenAPI schema cache. Test-only helper."""
    global _openapi_cache, _openapi_cache_at, _openapi_cache_key
    with _openapi_cache_lock:
        _openapi_cache = None
        _openapi_cache_at = 0.0
        _openapi_cache_key = None
        _openapi_inflight.clear()


def _find_kind_def(schema: dict, kind: str, api_version: str | None) -> dict | None:
    """Find the schema entry for `kind`, optionally scoped to api_version."""
    candidates = []
    for k, v in schema.items():
        if not isinstance(v, dict):
            continue
        # Kubernetes model names look like "io.k8s.api.apps.v1.Deployment".
        # We match by checking whether the kind name and api_version tokens appear.
        parts = k.lower().split(".")
        if kind.lower() not in [p.split("_")[-1] for p in parts]:
            # Loose match: "Deployment" appears as the last segment.
            if not k.lower().endswith("." + kind.lower()):
                continue
        if api_version:
            av = api_version.lower().replace("/", ".").replace(":", ".")
            if av not in k.lower():
                continue
        candidates.append((k, v))
    if not candidates:
        return None
    # Prefer the shortest matching name (most specific).
    candidates.sort(key=lambda kv: len(kv[0]))
    return candidates[0][1]


def _top_field_names(kind_def: dict) -> list[str]:
    return list((kind_def.get("properties") or {}).keys())


def _drill(kind_def: dict, path: str) -> dict | None:
    """Walk a dotted field path (no array indices) and return the inner schema."""
    node = kind_def
    for seg in [s for s in path.split(".") if s]:
        if not isinstance(node, dict):
            return None
        props = node.get("properties") or {}
        if seg not in props:
            return None
        node = props[seg]
    return node if isinstance(node, dict) else None


def _explain_kind(kind: str, kind_def: dict) -> str:
    top = _top_field_names(kind_def)
    description = (
        kind_def.get("description")
        or kind_def.get("x-kubernetes-group-version-kind", [{}])[0].get("description", "")
        or ""
    )
    lines = [f"kind: {kind}", f"description: {description or '(none)'}"]
    if top:
        lines.append("fields:")
        for f in top[:50]:  # cap to keep response bounded
            t = (kind_def.get("properties") or {}).get(f, {}).get("type") or "object"
            desc = ((kind_def.get("properties") or {}).get(f, {}).get("description") or "")[:120]
            lines.append(f"  - {f}: {t}{(' — ' + desc) if desc else ''}")
        if len(top) > 50:
            lines.append(f"  ... +{len(top) - 50} more fields; pass field_path=... to drill in")
    return "\n".join(lines)


def _explain_field(kind: str, path: str, field_def: dict) -> str:
    ftype = field_def.get("type") or "object"
    desc = field_def.get("description") or "(no description)"
    children = list((field_def.get("properties") or {}).keys())
    lines = [
        f"{kind} / {path}",
        f"type: {ftype}",
        f"description: {desc}",
    ]
    if children:
        lines.append(f"children: {', '.join(children[:30])}")
        if len(children) > 30:
            lines.append(f"  ... +{len(children) - 30} more")
    return "\n".join(lines)


# =============================================================================
# find_images — "which workloads are using image X (or matching substring)?"
# =============================================================================


_WORKLOAD_IMAGE_KINDS = ("Deployment", "StatefulSet", "DaemonSet")


def find_images(
    image_substring: str,
    namespace: str | None = None,
    kinds: list[str] | None = None,
) -> str:
    """🔍 FIND IMAGES — list every workload whose containers reference an
    image matching `image_substring` (case-insensitive substring match).

    Use case: "which Deployments are still on nginx:1.21?" or "which
    workloads reference my internal registry?" — answers in one call
    instead of `list_resources` + N × `get_resource_yaml`.

    Searches across Deployment / StatefulSet / DaemonSet (and any custom
    kinds you pass via `kinds=`) by walking
    `spec.template.spec.containers[*].image` and init containers.

    Args:
        image_substring: case-insensitive substring to match against
            container image strings. e.g. "nginx:1.21", "1.25.3",
            "registry.internal/library/".
        namespace: limit to one namespace. None = all namespaces.
        kinds: workload kinds to search. Default: Deployment, StatefulSet,
            DaemonSet. Pass a list like `["Deployment", "StatefulSet"]`
            to narrow.

    Returns a KIND / NAMESPACE / NAME / CONTAINER / IMAGE table.
    """
    from . import generic

    if not image_substring:
        raise ValueError("image_substring is required")
    needle = image_substring.lower()
    target_kinds = list(kinds) if kinds else list(_WORKLOAD_IMAGE_KINDS)

    dc = generic._dyn_client()
    rows: list[dict[str, str]] = []
    for kind in target_kinds:
        try:
            resource = generic._resource_for_kind(dc, kind)
        except (ValueError, Exception):
            # Unknown kind for this cluster — skip silently rather than
            # blanking the whole report.
            continue
        try:
            ret = resource.get(namespace=namespace) if namespace else resource.get()
            items = list(ret.items)
        except Exception as e:  # noqa: BLE001
            logger.debug("find_images: skipping %s: %s", kind, e)
            continue
        for item in items:
            obj = generic._to_dict(item)
            spec = (obj.get("spec", {}) or {}).get("template", {}) or {}
            pod_spec = spec.get("spec", {}) or {}
            for c in pod_spec.get("containers", []) or []:
                img = c.get("image", "")
                if needle in img.lower():
                    md = obj.get("metadata", {}) or {}
                    rows.append({
                        "KIND": kind,
                        "NAMESPACE": md.get("namespace", ""),
                        "NAME": md.get("name", "?"),
                        "CONTAINER": c.get("name", "?"),
                        "IMAGE": img,
                    })
            for c in pod_spec.get("initContainers", []) or []:
                img = c.get("image", "")
                if needle in img.lower():
                    md = obj.get("metadata", {}) or {}
                    rows.append({
                        "KIND": kind,
                        "NAMESPACE": md.get("namespace", ""),
                        "NAME": md.get("name", "?"),
                        "CONTAINER": f"[init] {c.get('name', '?')}",
                        "IMAGE": img,
                    })

    if not rows:
        return f"(no workloads reference an image matching {image_substring!r})"
    return short_table(rows, ["KIND", "NAMESPACE", "NAME", "CONTAINER", "IMAGE"])


def register(mcp) -> None:
    mcp.tool()(get_api_resources)
    mcp.tool()(explain_resource)
    mcp.tool()(find_images)
