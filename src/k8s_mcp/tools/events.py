"""Event listing.

中文说明：
拉取 K8s Event 对象，常用于排障。`warning_only=True` 只返回 Warning
类事件，过滤掉千篇一律的 Normal 事件；`field_selector` 支持按
`involvedObject.kind`、`reason` 等字段过滤。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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
    with_ts: bool = False,
) -> list[dict]:
    """Shared implementation of list_events / get_events_for_object.

    Filters BEFORE slicing so `warning_only=True` cannot silently drop
    Warning events that sorted below the top `limit` Normal events
    (which would have been the case if we'd sliced first).

    `with_ts=True` attaches an internal `_ts` (datetime) field to each row.
    Callers that need to merge across multiple namespaces use it to
    re-sort the combined result by recency before truncating — formatted
    LAST-SEEN strings don't sort correctly. `short_table` ignores
    unknown columns, so the `_ts` field never reaches the rendered output.
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
        ts = e.last_timestamp or e.first_timestamp or e.event_time or e.metadata.creation_timestamp
        row = {
            "TYPE": etype,
            "REASON": e.reason or "",
            "OBJECT": f"{obj.kind}/{obj.name}" if obj else "",
            "MESSAGE": (e.message or "")[:80],
            "LAST-SEEN": format_relative_time(e.last_timestamp or e.first_timestamp),
            "COUNT": str(e.count or 1),
        }
        if with_ts:
            row["_ts"] = ts or _epoch_zero()
        rows.append(row)
    return rows


def list_events(
    namespace: str | None = None,
    field_selector: str | None = None,
    warning_only: bool = False,
    limit: int = 50,
    *,
    namespaces: list[str] | None = None,
) -> str:
    """List Kubernetes Events.

    Use this for cluster-wide or namespace-wide event streams. For events
    on a single object, prefer `get_events_for_object(kind=..., name=...)`
    — it pushes the `involvedObject` filter to the apiserver and is much
    faster than fetching all events and grepping client-side.

    Note: prefer reusing the most recent result for the same query rather
    than re-calling if the underlying state is unlikely to have changed. New
    calls remain valid when verifying a mutation's effect.

    Args:
        namespace: namespace to list events from; None = all namespaces.
            For multi-namespace queries, prefer `namespaces` so the
            result isn't silently broadened to cluster-wide.
        namespaces: explicit list of namespaces. When 2+ are given,
            queries fan out per-namespace in parallel and merge by
            last-seen desc before truncating to `limit`. Empty list
            returns "(no events)". Takes precedence over `namespace`.
        field_selector: e.g. "involvedObject.name=my-pod",
            "involvedObject.kind=Deployment".
        warning_only: if True, only show Warning type events. Filtering
            happens BEFORE the `limit` slice so Warning events never get
            silently dropped by a sea of Normal events.
        limit: max rows to return (default 50). Events are returned sorted by
            lastTimestamp descending.

    Returns a TABLE: TYPE / REASON / OBJECT / MESSAGE / LAST-SEEN / COUNT.
    """
    if namespaces is not None:
        if not namespaces:
            return "(no events)"
        if len(namespaces) == 1:
            namespace = namespaces[0]
        else:
            return _list_events_multi(namespaces, warning_only, limit)
    rows = _collect_events(
        namespace=namespace,
        field_selector=field_selector,
        warning_only=warning_only,
        limit=limit,
    )
    if not rows:
        return "(no events)"
    return short_table(rows, ["TYPE", "REASON", "OBJECT", "MESSAGE", "LAST-SEEN", "COUNT"])


# Cap on concurrent apiserver event fetches when fanning out across
# namespaces. Same rationale as logs._MULTI_PARALLEL_MAX_WORKERS.
_MULTI_NAMESPACES_MAX_WORKERS = 8


def _list_events_multi(
    namespaces: list[str], warning_only: bool, limit: int,
) -> str:
    """Fan out event fetches across multiple namespaces, merge by recency.

    Each per-namespace query asks for `limit` rows so the merged set
    has enough material to draw a globally-recent top-`limit` from —
    otherwise a noisy namespace could crowd out a quieter one with
    older-but-still-relevant events.
    """
    def _fetch(ns: str) -> list[dict]:
        return _collect_events(
            namespace=ns,
            field_selector=None,
            warning_only=warning_only,
            limit=limit,
            with_ts=True,
        )

    with ThreadPoolExecutor(
        max_workers=min(_MULTI_NAMESPACES_MAX_WORKERS, len(namespaces)),
    ) as ex:
        per_ns = list(ex.map(_fetch, namespaces))

    merged = [r for rows in per_ns for r in rows]
    merged.sort(key=lambda r: r["_ts"], reverse=True)
    merged = merged[:limit]
    # Drop the internal _ts field before formatting; short_table would
    # silently ignore it but we'd rather not leak the column name into
    # any future refactor that passes the rows dict around.
    for r in merged:
        r.pop("_ts", None)

    if not merged:
        return "(no events)"
    return short_table(merged, ["TYPE", "REASON", "OBJECT", "MESSAGE", "LAST-SEEN", "COUNT"])


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
