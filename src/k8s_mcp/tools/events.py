"""Event listing.

中文说明：
拉取 K8s Event 对象，常用于排障。`warning_only=True` 只返回 Warning
类事件，过滤掉千篇一律的 Normal 事件；`field_selector` 支持按
`involvedObject.kind`、`reason` 等字段过滤。
"""
from __future__ import annotations

from datetime import UTC

from ..client import get_api_client
from ..formatters import short_table


def _core_v1():
    from kubernetes import client
    return client.CoreV1Api(get_api_client())


def list_events(
    namespace: str | None = None,
    field_selector: str | None = None,
    warning_only: bool = False,
    limit: int = 50,
) -> str:
    """List Kubernetes Events.

    Args:
        namespace: namespace to list events from; None = all namespaces.
        field_selector: e.g. "involvedObject.name=my-pod",
            "involvedObject.kind=Deployment".
        warning_only: if True, only show Warning type events.
        limit: max rows to return (default 50). Events are returned sorted by
            lastTimestamp descending.

    Returns a TABLE: TYPE / REASON / OBJECT / MESSAGE / LAST-SEEN / COUNT.
    """
    api = _core_v1()
    if namespace:
        ret = api.list_namespaced_event(namespace, field_selector=field_selector)
    else:
        ret = api.list_event_for_all_namespaces(field_selector=field_selector)

    events = list(ret.items)
    # Sort by last_timestamp desc (use first_timestamp as fallback)
    def _ts(e):
        lt = e.last_timestamp or e.first_timestamp or e.event_time or e.metadata.creation_timestamp
        return lt or _epoch_zero()

    events.sort(key=_ts, reverse=True)
    events = events[:limit]

    rows = []
    for e in events:
        etype = e.type or "Normal"
        if warning_only and etype != "Warning":
            continue
        obj = e.involved_object
        rows.append({
            "TYPE": etype,
            "REASON": e.reason or "",
            "OBJECT": f"{obj.kind}/{obj.name}" if obj else "",
            "MESSAGE": (e.message or "")[:80],
            "LAST-SEEN": _format_time(e.last_timestamp or e.first_timestamp),
            "COUNT": str(e.count or 1),
        })

    if not rows:
        return "(no events)"
    return short_table(rows, ["TYPE", "REASON", "OBJECT", "MESSAGE", "LAST-SEEN", "COUNT"])


def _format_time(ts) -> str:
    if not ts:
        return ""
    from datetime import datetime
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - ts
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _epoch_zero():
    from datetime import datetime
    return datetime(1970, 1, 1, tzinfo=UTC)


def register(mcp) -> None:
    mcp.tool()(list_events)
