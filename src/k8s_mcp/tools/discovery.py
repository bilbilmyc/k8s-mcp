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
    OpenAPI v3 schema 反查 kind / 字段的定义与描述，等价于
    `kubectl explain`。

两个工具都只读，自动绕开 namespace allowlist（只读不需要守门）。
"""
from __future__ import annotations

import logging

from kubernetes import client

from ..client import get_api_client
from ..formatters import short_table

logger = logging.getLogger(__name__)


def get_api_resources(prefix: str | None = None) -> str:
    """List all API resources discoverable in the cluster.

    Args:
        prefix: optional filter, e.g. "deploy" → matches Deployment, etc.

    Returns a NAME / APIVERSION / NAMESPACED / KIND table — same fields as
    `kubectl api-resources`.

    Includes CRDs (custom resources registered in the cluster), so this is
    the right way to ask "what kinds exist here?".
    """
    api = client.ApisApi(get_api_client())
    rows: list[dict[str, str]] = []

    # Each "group" in /apis returns its own resource list.
    # (Note: the core v1 group is at /api not /apis.)
    groups = [g["name"] for g in api.get_api_versions().groups]
    for group in groups:
        try:
            resources = api.get_api_resources(group).resources or []
        except Exception as e:  # noqa: BLE001 — surface one bad group, keep going
            logger.debug("api_resources: skipping group %s: %s", group, e)
            continue
        for r in resources:
            name = (r.get("name") or "").strip()
            kind = (r.get("kind") or "").strip()
            api_version = f"{group}/{(r.get('version') or '').strip()}"
            if not name or not kind:
                continue
            if prefix and prefix.lower() not in name.lower() and prefix.lower() not in kind.lower():
                continue
            namespaced = "true" if r.get("namespaced") else "false"
            short_names = ",".join(r.get("shortNames") or [])
            verbs = ",".join((r.get("verbs") or [])[:4])  # cap to first 4 verbs
            rows.append({
                "NAME": name,
                "SHORTNAMES": short_names,
                "APIVERSION": api_version,
                "NAMESPACED": namespaced,
                "KIND": kind,
                "VERBS": verbs,
            })

    # Also include the core v1 group
    core_resources = _core_api_resources()
    for r in core_resources:
        name = r["name"]
        kind = r["kind"]
        if prefix and prefix.lower() not in name.lower() and prefix.lower() not in kind.lower():
            continue
        rows.append({
            "NAME": name,
            "SHORTNAMES": r.get("shortName", ""),
            "APIVERSION": "v1",
            "NAMESPACED": "true",
            "KIND": kind,
            "VERBS": r.get("verbs", ""),
        })

    if not rows:
        return f"(no API resources match prefix={prefix!r})"
    rows.sort(key=lambda x: (x["APIVERSION"], x["KIND"]))
    return short_table(rows, ["NAME", "SHORTNAMES", "APIVERSION", "NAMESPACED", "KIND", "VERBS"])


def _core_api_resources() -> list[dict[str, str]]:
    """Hardcoded list of Core v1 resources (the /api endpoint is not always
    exposed as a discovery list by python-client)."""
    return [
        {"name": "pods", "shortName": "po", "kind": "Pod", "verbs": "get list watch create delete"},
        {"name": "services", "shortName": "svc", "kind": "Service", "verbs": "get list watch create delete"},
        {"name": "configmaps", "shortName": "cm", "kind": "ConfigMap", "verbs": "get list watch create delete"},
        {"name": "secrets", "kind": "Secret", "verbs": "get list watch create delete"},
        {"name": "namespaces", "shortName": "ns", "kind": "Namespace", "verbs": "get list watch create delete"},
        {"name": "nodes", "shortName": "no", "kind": "Node", "verbs": "get list watch create delete"},
        {"name": "persistentvolumes", "shortName": "pv", "kind": "PersistentVolume", "verbs": "get list watch create delete"},
        {"name": "persistentvolumeclaims", "shortName": "pvc", "kind": "PersistentVolumeClaim", "verbs": "get list watch create delete"},
        {"name": "serviceaccounts", "shortName": "sa", "kind": "ServiceAccount", "verbs": "get list watch create delete"},
        {"name": "endpoints", "shortName": "ep", "kind": "Endpoints", "verbs": "get list watch"},
        {"name": "events", "shortName": "ev", "kind": "Event", "verbs": "get list watch"},
        {"name": "replicationcontrollers", "shortName": "rc", "kind": "ReplicationController", "verbs": "get list watch create delete"},
        {"name": "resourcequotas", "shortName": "quota", "kind": "ResourceQuota", "verbs": "get list watch create delete"},
        {"name": "limitranges", "shortName": "limits", "kind": "LimitRange", "verbs": "get list watch create delete"},
        {"name": "podtemplates", "kind": "PodTemplate", "verbs": "get list watch create delete"},
    ]


# =============================================================================
# explain_resource — kubectl explain via OpenAPI v3 schema
# =============================================================================


def explain_resource(
    kind: str,
    field_path: str | None = None,
    api_version: str | None = None,
) -> str:
    """Look up the schema (and description) for a Kind or a nested field.

    Args:
        kind: e.g. "Pod", "Deployment", "HorizontalPodAutoscaler".
        field_path: optional dotted path into the resource, e.g.
            "spec.template.spec.containers". When omitted, returns the
            top-level description and a list of top-level fields.
        api_version: optional, e.g. "apps/v1". When omitted, the first
            matching definition in the OpenAPI schema is used.

    Equivalent to `kubectl explain <kind>[.<field_path>]`. LLM agents
    should call this before writing a YAML manifest for a kind they
    don't know.

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


_openapi_cache: dict | None = None


def _get_openapi_schema() -> dict:
    """Fetch and cache the cluster's OpenAPI v3 schema (lazy)."""
    global _openapi_cache
    if _openapi_cache is None:
        spec = client.OpenApiApi(get_api_client()).get_openapi_spec()
        # The spec is a nested dict under "components"/"schemas" in v3.
        _openapi_cache = (
            spec.get("components", {}).get("schemas", {})
            if isinstance(spec, dict)
            else {}
        )
    return _openapi_cache


def _find_kind_def(schema: dict, kind: str, api_version: str | None) -> dict | None:
    """Find the schema entry for `kind`, optionally scoped to api_version."""
    candidates = []
    for k, v in schema.items():
        if not isinstance(v, dict):
            continue
        # OpenAPI v3 model names look like "io.k8s.api.apps.v1.Deployment".
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


def register(mcp) -> None:
    mcp.tool()(get_api_resources)
    mcp.tool()(explain_resource)
