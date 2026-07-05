"""`kubectl wait` equivalent — poll a resource until a condition is met.

Supports the two forms kubectl uses:

  - `--for=condition=<name>`: poll `status.conditions[].type==<name>`
    and accept when `status=="True"`. Pods, Deployments, StatefulSets,
    ReplicaSets, etc. all expose a `.status.conditions` array.
  - `--for=jsonpath=<expr>=<value>`: poll a JSONPath expression and
    accept when the result equals the expected value (string-compared).

Polling every second, with a configurable timeout. Returns when satisfied,
raises TimeoutError on timeout.

中文说明：
`wait_resource(kind, name, namespace, for_condition=..., for_jsonpath=...,
jsonpath_value=..., timeout_seconds=60)`：

  - 两种 for_* 模式：condition 看 status.conditions[].type + status==True；
    jsonpath 比对完整 jsonpath 表达式的字符串结果。
  - 默认每秒轮询，超时抛 TimeoutError。
  - 适合 Agent "apply 后等 rollout 完成" 这类编排流程。
"""
from __future__ import annotations

import logging
import re
import time

from kubernetes.client.rest import ApiException

from ..client import get_api_client
from .generic import _api_version_for

logger = logging.getLogger(__name__)


def _dyn():
    from kubernetes import dynamic
    return dynamic.DynamicClient(get_api_client())


def wait_resource(
    kind: str,
    name: str,
    namespace: str | None = None,
    for_condition: str | None = None,
    for_jsonpath: str | None = None,
    jsonpath_value: str | None = None,
    timeout_seconds: int = 60,
) -> str:
    """⚠️ BLOCKING — polls the resource every second until the condition is met
    or `timeout_seconds` elapses (whichever comes first). The agent stays
    blocked for the full duration when the condition is slow to appear.

    Use `timeout_seconds` to bound the wait. To get a snapshot instead of
    waiting, use `get_resource` and inspect status manually. For Pod
    startup specifically, a smaller timeout (10–20s) usually suffices.

    Args:
        kind: resource kind (e.g. "Pod", "Deployment").
        name: resource name.
        namespace: namespace; required for namespaced kinds.
        for_condition: condition name to wait for (status.conditions[type=name].status == "True").
            Example: "Ready", "Available".
        for_jsonpath: JSONPath expression into the resource; must be paired with
            `jsonpath_value`. Example: "status.replicas" — must equal "3".
        jsonpath_value: expected string value of the JSONPath expression.
        timeout_seconds: poll timeout (default 60s).

    Exactly one of (for_condition) or (for_jsonpath + jsonpath_value) must be set.

    Returns a short status line; raises TimeoutError on timeout.
    """
    if not for_condition and not for_jsonpath:
        raise ValueError("Provide for_condition=... or for_jsonpath=... + jsonpath_value=...")
    if for_condition and for_jsonpath:
        raise ValueError("for_condition and for_jsonpath are mutually exclusive; pick one")
    if for_jsonpath and jsonpath_value is None:
        raise ValueError("for_jsonpath requires jsonpath_value")

    deadline = time.monotonic() + timeout_seconds
    dc = _dyn()

    api_version = _api_version_for(kind)
    try:
        resource = dc.resources.get(api_version=api_version, kind=kind)
    except Exception as e:
        raise ValueError(f"Unknown kind: {kind}") from e

    while True:
        try:
            item = resource.get(name=name, namespace=namespace)
        except ApiException as e:
            if e.status == 404:
                raise LookupError(f"{kind} '{name}' not found in namespace '{namespace}'") from e
            raise

        obj = item.to_dict() if hasattr(item, "to_dict") else dict(item)

        if for_condition:
            met = _check_condition(obj, for_condition)
            if met:
                return f"{kind}/{name}: condition '{for_condition}' met"
        else:
            actual = _jsonpath(obj, for_jsonpath)
            met = (str(actual) == str(jsonpath_value))
            if met:
                return (
                    f"{kind}/{name}: jsonpath '{for_jsonpath}' = '{jsonpath_value}' (actual: {actual!r})"
                )

        if time.monotonic() > deadline:
            raise TimeoutError(
                f"Timeout after {timeout_seconds}s waiting for {kind}/{name} "
                f"({'condition=' + for_condition if for_condition else 'jsonpath=' + for_jsonpath})"
            )
        time.sleep(1)


# ---------- internals ----------------------------------------------------------


def _check_condition(obj: dict, condition: str) -> bool:
    """Return True if status.conditions has type=condition with status=True."""
    conds = (obj.get("status") or {}).get("conditions") or []
    for c in conds:
        if c.get("type") == condition and str(c.get("status")) == "True":
            return True
    return False


def _jsonpath(obj: dict, expr: str) -> object:
    """Tiny JSONPath implementation.

    Supports the common kubectl subset:
      - dotted field paths: `status.replicas`
      - bracketed array index: `spec.containers[0].image`
      - everything together: `spec.containers[-1].image`

    Walks `obj` according to the expression. Returns the final value (raises
    LookupError if the path is missing or wrong type).
    """
    cur: object = obj
    tokens = re.findall(r"\.[^.\[]+|\[\-?\d+\]", expr)
    if not expr.startswith("."):
        head, _, rest = expr.partition(".")
        if head:
            if not isinstance(cur, dict) or head not in cur:
                raise LookupError(f"jsonpath: key '{head}' not found")
            cur = cur[head]
        tokens = re.findall(r"\.[^.\[]+|\[\-?\d+\]", "." + rest)
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("."):
            key = tok[1:]
            if not isinstance(cur, dict) or key not in cur:
                raise LookupError(f"jsonpath: key '{key}' not found at token {i}")
            cur = cur[key]
        elif tok.startswith("["):
            idx = int(tok[1:-1])
            if not isinstance(cur, list):
                raise LookupError(f"jsonpath: not a list at token {i}")
            cur = cur[idx]
        i += 1
    return cur


def register(mcp) -> None:
    mcp.tool()(wait_resource)
