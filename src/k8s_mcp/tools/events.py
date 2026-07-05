"""Event listing.

中文说明：
拉取 K8s Event 对象，常用于排障。`warning_only=True` 只返回 Warning
类事件，过滤掉千篇一律的 Normal 事件；`field_selector` 支持按
`involvedObject.kind`、`reason` 等字段过滤。
"""
from __future__ import annotations

from datetime import UTC, datetime

from ..client import get_api_client
from ..formatters import format_relative_time, short_table


def _core_v1():
    from kubernetes import client
    return client.CoreV1Api(get_api_client())


def _epoch_zero():
    return datetime(1970, 1, 1, tzinfo=UTC)


def _ts(e):
    """Sort key: latest of last/first/event_time/creation_timestamp."""
    lt = e.last_timestamp or e.first_timestamp or e.event_time or e.metadata.creation_timestamp
    return lt or _epoch_zero()


def _collect_events(
    *,
    namespace: str | None,
    field_selector: str | None,
    warning_only: bool,
    limit: int,
) -> list[dict]:
    """Shared implementation of list_events / get_events_for_object.

    Filters BEFORE slicing so `warning_only=True` cannot silently drop
    Warning events that sorted below the top `limit` Normal events
    (which would have been the case if we'd sliced first).
    """
    api = _core_v1()
    if namespace:
        ret = api.list_namespaced_event(namespace, field_selector=field_selector)
    else:
        ret = api.list_event_for_all_namespaces(field_selector=field_selector)

    events = list(ret.items)
    if warning_only:
        events = [e for e in events if (e.type or "Normal") == "Warning"]
    events.sort(key=_ts, reverse=True)
    events = events[:limit]

    rows: list[dict] = []
    for e in events:
        etype = e.type or "Normal"
        obj = e.involved_object
        rows.append({
            "TYPE": etype,
            "REASON": e.reason or "",
            "OBJECT": f"{obj.kind}/{obj.name}" if obj else "",
            "MESSAGE": (e.message or "")[:80],
            "LAST-SEEN": format_relative_time(e.last_timestamp or e.first_timestamp),
            "COUNT": str(e.count or 1),
        })
    return rows


def list_events(
    namespace: str | None = None,
    field_selector: str | None = None,
    warning_only: bool = False,
    limit: int = 50,
) -> str:
    """List Kubernetes Events.

    Note: prefer reusing the most recent result for the same query rather
    than re-calling if the underlying state is unlikely to have changed. New
    calls remain valid when verifying a mutation's effect.

    Args:
        namespace: namespace to list events from; None = all namespaces.
        field_selector: e.g. "involvedObject.name=my-pod",
            "involvedObject.kind=Deployment".
        warning_only: if True, only show Warning type events. Filtering
            happens BEFORE the `limit` slice so Warning events never get
            silently dropped by a sea of Normal events.
        limit: max rows to return (default 50). Events are returned sorted by
            lastTimestamp descending.

    Returns a TABLE: TYPE / REASON / OBJECT / MESSAGE / LAST-SEEN / COUNT.
    """
    rows = _collect_events(
        namespace=namespace,
        field_selector=field_selector,
        warning_only=warning_only,
        limit=limit,
    )
    if not rows:
        return "(no events)"
    return short_table(rows, ["TYPE", "REASON", "OBJECT", "MESSAGE", "LAST-SEEN", "COUNT"])


def get_events_for_object(
    kind: str,
    name: str,
    namespace: str | None = None,
    limit: int = 50,
) -> str:
    """📜 EVENTS FOR OBJECT — list every Event whose `involvedObject` matches
    the given (kind, name). Use this for "why is X failing?" — instead of
    scanning a namespace-wide event stream and grepping mentally.

    Args:
        kind: object Kind, e.g. "Pod", "Deployment", "StatefulSet",
            "ReplicaSet", "PersistentVolumeClaim".
        name: object name.
        namespace: object namespace. Required for namespaced kinds; pass
            None for cluster-scoped kinds (Node, PersistentVolume).
        limit: max events to return (default 50). Sorted by last-seen
            desc, so the latest signal is at the top.

    Returns a TYPE / REASON / MESSAGE / LAST-SEEN / COUNT table — same
    shape as `list_events`. Empty result returns "(no events)" rather
    than an empty table, so the agent doesn't misread "no data" as
    "tool failed".
    """
    rows = _collect_events(
        namespace=namespace,
        field_selector=f"involvedObject.kind={kind},involvedObject.name={name}",
        warning_only=False,
        limit=limit,
    )
    if not rows:
        return (
            f"(no events for {kind}/{name}"
            f"{' in namespace ' + namespace if namespace else ''})"
        )
    return short_table(rows, ["TYPE", "REASON", "OBJECT", "MESSAGE", "LAST-SEEN", "COUNT"])


def register(mcp) -> None:
    mcp.tool()(list_events)
    mcp.tool()(get_events_for_object)
