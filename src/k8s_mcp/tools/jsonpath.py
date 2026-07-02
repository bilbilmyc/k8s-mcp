"""`kubectl get -o jsonpath=...` style field extraction.

This is the cheap alternative to parsing the full YAML: when an agent only
needs `status.phase`, `spec.replicas`, or similar, jsonpath returns just
that value (no large body in context).

Supports a useful subset of JSONPath — the parts LLMs actually use:
  - dotted field paths: `status.phase`
  - bracketed array index: `spec.containers[0].image`
  - optional list mode: when `name` is omitted, extract from a *list* of
    matching resources (one value per line).

Reference: https://kubernetes.io/docs/reference/kubectl/jsonpath/
"""
from __future__ import annotations

import logging

from kubernetes import dynamic
from kubernetes.client.rest import ApiException

from ..client import get_api_client
from .wait_tool import _api_version_for, _jsonpath

logger = logging.getLogger(__name__)


def _dyn():
    return dynamic.DynamicClient(get_api_client())


def get_resource_jsonpath(
    kind: str,
    path: str,
    name: str | None = None,
    namespace: str | None = None,
    label_selector: str | None = None,
) -> str:
    """Extract one or more field values from a resource via JSONPath.

    Args:
        kind: resource kind (e.g. "Pod", "Deployment").
        path: simple JSONPath expression (dotted/bracketed, see module doc).
        name: resource name; omit to extract from all matching resources in
            the namespace (or cluster-wide when namespace is also None).
        namespace: namespace to search.
        label_selector: when listing, filter by label selector.

    Returns the value as text:
      - Single resource → the matched value (or "(empty)" if null).
      - Multiple resources (list mode) → one value per line.

    Raises LookupError if the field doesn't exist.
    """
    dc = _dyn()
    api_version = _api_version_for(kind)
    try:
        resource = dc.resources.get(api_version=api_version, kind=kind)
    except Exception as e:
        raise ValueError(f"Unknown kind: {kind}") from e

    items: list[dict] = []
    if name:
        try:
            item = resource.get(name=name, namespace=namespace)
        except ApiException as e:
            if e.status == 404:
                raise LookupError(f"{kind} '{name}' not found in '{namespace}'") from e
            raise
        items = [item.to_dict() if hasattr(item, "to_dict") else dict(item)]
    else:
        kwargs = {}
        if label_selector:
            kwargs["label_selector"] = label_selector
        if namespace:
            ret = resource.get(namespace=namespace, **kwargs)
        else:
            ret = resource.get(**kwargs)
        items = [item.to_dict() if hasattr(item, "to_dict") else dict(item) for item in ret.items]

    if not items:
        return "(no resources matched)"

    values = []
    for obj in items:
        try:
            v = _jsonpath(obj, path)
        except LookupError as e:
            raise LookupError(f"{kind}: {e}") from e
        values.append("" if v is None else str(v))

    return "\n".join(values)


def register(mcp) -> None:
    mcp.tool()(get_resource_jsonpath)
