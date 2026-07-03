"""Pod log retrieval with filtering, multi-pod, and size-bounded output.

Designed for long-running pods (days/weeks of logs) where a naive `kubectl
logs` would return hundreds of MB. The tool defaults are safe:

  - tail_lines defaults to settings.default_tail_lines (100)
  - max_bytes defaults to 1 MB; the response is truncated with a notice
  - pattern (regex) + context_lines lets the agent narrow to relevant lines
  - label_selector switches to multi-pod mode and prefixes each line with the
    pod name
  - since_time / until_time: 绝对时间窗口（RFC3339），用于"两点到四点"
    这类查询；K8s API 仅支持下界，上界客户端过滤
  - strict_time: True 时丢弃没有 RFC3339 时间戳的行（部分容器不打印
    时间戳，会让用户误以为"没数据"）

中文说明：
`get_pod_logs` 是 k8s-mcp 里被调用最频繁的工具，参数最多也最易踩坑。
关键设计点：

  - 全部过滤都在客户端做（pattern/context/since_time/until_time 都是），
    K8s API 只负责返回原始流 + sinceTime 下界。所以"先拉再过滤"是
    常规路径，tail_lines 不要无脑拉太大，建议配合 pattern 收窄。
  - 空结果返回中文友好的"没有日志"提示（不是空字符串——Cherry Studio
    等客户端会隐藏空 tool 输出）。
  - label_selector 模式自动 list_pods 再逐个 fetch logs，跨 Pod 的多行
    输出每行都会前缀 `[pod-name]`。
  - output_format=json 返回 [{pod, container, time, line}] 列表，便于
    Agent 程序化处理。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from kubernetes.client.rest import ApiException

from ..client import get_api_client

logger = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 1_048_576  # 1 MiB
MAX_BYTES_HARD_CAP = 16_000_000  # 16 MiB — refuse beyond this

_RFC3339_EXAMPLES = (
    "Examples: '2026-07-02T14:00:00Z', '2026-07-02T14:00:00+08:00', "
    "'2026-07-02T14:00:00.123456Z'"
)


def _core_v1():
    from kubernetes import client
    return client.CoreV1Api(get_api_client())


def get_pod_logs(
    pod_name: str | None = None,
    namespace: str = "default",
    container: str | None = None,
    label_selector: str | None = None,
    tail_lines: int | None = None,
    since_seconds: int | None = None,
    since_time: str | None = None,
    until_time: str | None = None,
    strict_time: bool = False,
    previous: bool = False,
    timestamps: bool = False,
    pattern: str | None = None,
    context_lines: int = 0,
    max_bytes: int = DEFAULT_MAX_BYTES,
    output_format: str = "text",
) -> str:
    """Fetch Pod logs with safe defaults. Equivalent to `kubectl logs`, with
    added filtering on top. Defaults: `tail_lines` from settings
    (default_tail_lines), `max_bytes` 1 MiB (16 MiB hard cap — the response
    is truncated past the cap with a footer notice telling you to narrow).

    ⚠️ BLOCKING on `since_time` / `since_seconds` — K8s streams the entire
    log from the start of the pod (not from the lower bound); the bound is
    applied server-side AFTER the stream finishes. For very chatty pods,
    `tail_lines` + `pattern` is faster than `since_seconds`.

    Args:

    Args:
        pod_name: pod name (mutually exclusive with label_selector).
        namespace: pod's namespace.
        container: container name (multi-container pods only).
        label_selector: e.g. "app=nginx". When set, fetches logs from all
            matching pods and prefixes each line with `[pod-name]`. Overrides
            pod_name.
        tail_lines: number of recent lines to return per pod. Default comes
            from settings.default_tail_lines. Set to a large number (e.g.
            10000) only if you've also narrowed with `pattern`.
        since_seconds: only return logs newer than this many seconds.
            Mutually exclusive with `since_time`.
        since_time: RFC3339 absolute lower bound, e.g. "2026-07-02T14:00:00Z".
            Forces `timestamps=True` internally. Mutually exclusive with
            `since_seconds`. The K8s API supports this natively.
        until_time: RFC3339 absolute upper bound. Forces `timestamps=True`
            internally. K8s API does NOT support an upper bound, so this
            is enforced client-side after fetch. The `strict_time` flag
            controls how lines without parseable timestamps are handled.
        strict_time: when True, drop any record whose timestamp cannot be
            parsed. Default False: keep un-timestamped records (useful for
            pods whose containers don't emit RFC3339 timestamps). Only
            meaningful when `since_time` or `until_time` is set.
        previous: previous (terminated) container instance, for crash debug.
        timestamps: prefix each line with RFC3339 timestamp.
        pattern: regex; only lines matching (or near matches when
            context_lines > 0) are returned. Server-side filter is NOT
            possible — we filter client-side after fetching, so use a small
            tail_lines unless you need wide context.
        context_lines: when `pattern` is set, include this many lines
            before and after each match.
        max_bytes: hard cap on the returned payload. Default 1 MiB. The
            response is truncated if exceeded and a notice is appended.
        output_format: "text" (default) or "json". JSON returns a list of
            {pod, container, time, line} records.

    Returns log text (or JSON). Truncation is reported as a footer line:
        "... [truncated N bytes; total M bytes fetched]"
    """
    if pod_name and label_selector:
        raise ValueError("pod_name and label_selector are mutually exclusive")
    if not pod_name and not label_selector:
        raise ValueError("One of pod_name or label_selector is required")
    if max_bytes > MAX_BYTES_HARD_CAP:
        raise ValueError(f"max_bytes may not exceed {MAX_BYTES_HARD_CAP} bytes")
    if output_format not in ("text", "json"):
        raise ValueError(f"output_format must be 'text' or 'json', got {output_format!r}")

    # Time-window validation. since_time and since_seconds are mutually
    # exclusive at the K8s API level; surface this locally with a clear
    # message so the agent doesn't get a confusing 400 from apiserver.
    if since_time and since_seconds is not None:
        raise ValueError(
            "since_time and since_seconds are mutually exclusive — "
            "pass one or the other, not both"
        )
    if strict_time and not (since_time or until_time):
        raise ValueError(
            "strict_time=True requires since_time and/or until_time to be set"
        )

    since_dt = _parse_rfc3339(since_time, field="since_time") if since_time else None
    until_dt = _parse_rfc3339(until_time, field="until_time") if until_time else None
    if since_dt and until_dt and until_dt < since_dt:
        raise ValueError(
            f"until_time ({until_time!r}) must be >= since_time ({since_time!r})"
        )

    # Time-window mode requires timestamps on every line so we can parse them.
    # The K8s API only adds timestamps when we ask; we force them on whenever
    # the agent has asked for any time-window filtering.
    if since_time or until_time:
        timestamps = True

    from ..config import get_settings
    settings = get_settings()
    if tail_lines is None:
        tail_lines = settings.default_tail_lines

    if label_selector:
        records = _fetch_logs_multi(
            label_selector, namespace, container, tail_lines, since_seconds,
            since_time, previous, timestamps,
        )
    else:
        records = _fetch_logs_single(
            pod_name, namespace, container, tail_lines, since_seconds,
            since_time, previous, timestamps,
        )

    if since_dt or until_dt:
        records = _filter_by_time(records, since_dt, until_dt, strict_time)

    if pattern:
        records = _filter_with_context(records, pattern, context_lines)

    payload, truncated = _serialize(records, max_bytes, output_format)
    if truncated:
        notice = (
            f"\n... [truncated: output exceeded {max_bytes} bytes; "
            f"narrow with tail_lines, since_seconds, since_time, until_time, or pattern]"
        )
        payload = payload + notice
        return payload

    if not records:
        # Avoid returning an empty string — many MCP clients (Cherry Studio,
        # etc.) hide empty tool results, making it look like the tool wasn't
        # called. Surface a helpful message instead.
        if since_dt or until_dt:
            return _empty_time_window_message(
                namespace, pod_name, label_selector, since_time, until_time, strict_time,
            )
        if pod_name:
            target = f"pod '{namespace}/{pod_name}'"
            hint = (
                " the container writes to a file rather than stdout/stderr, "
                "the pod just started, or tail_lines is too small."
            )
        else:
            target = f"pods matching label_selector in namespace '{namespace}'"
            hint = (
                " the containers write to a file rather than stdout/stderr, "
                "the pods just started, or tail_lines is too small."
            )
        return (
            f"(no log lines from {target}. Possible causes:{hint})"
        )
    return payload


# ---------- internals ----------------------------------------------------------


def _fetch_logs_single(
    pod_name: str, namespace: str, container: str | None,
    tail_lines: int, since_seconds: int | None,
    since_time: str | None,
    previous: bool, timestamps: bool,
) -> list[dict[str, str]]:
    kwargs: dict[str, Any] = {}
    if container:
        kwargs["container"] = container
    if tail_lines:
        kwargs["tail_lines"] = int(tail_lines)
    if since_seconds is not None:
        kwargs["since_seconds"] = int(since_seconds)
    if since_time:
        # python-client passes the string straight through to apiserver,
        # which expects RFC3339. We pass the original string (not a
        # datetime) so the apiserver does its own parsing — that way if the
        # value is invalid, we get the apiserver's clear error message
        # rather than a TypeError from our side.
        kwargs["since_time"] = since_time
    if previous:
        kwargs["previous"] = True
    if timestamps:
        kwargs["timestamps"] = True

    try:
        text = _core_v1().read_namespaced_pod_log(
            name=pod_name, namespace=namespace, **kwargs
        )
    except ApiException as e:
        if e.status == 404:
            raise LookupError(f"Pod '{namespace}/{pod_name}' not found") from e
        if e.status == 400:
            raise ValueError(
                f"Cannot fetch logs: {e.reason}. "
                + (
                    f"Available containers: {_list_containers(pod_name, namespace)}"
                    if "container" in (e.reason or "").lower() else ""
                )
            ) from e
        raise

    return _parse_lines(text, pod=pod_name, container=container or "")


def _fetch_logs_multi(
    label_selector: str, namespace: str, container: str | None,
    tail_lines: int, since_seconds: int | None,
    since_time: str | None,
    previous: bool, timestamps: bool,
) -> list[dict[str, str]]:
    pods_api = _core_v1()
    ret = pods_api.list_namespaced_pod(
        namespace, label_selector=label_selector,
    )
    pod_names = [p.metadata.name for p in ret.items]
    if not pod_names:
        return []
    records: list[dict[str, str]] = []
    for pn in pod_names:
        try:
            records.extend(_fetch_logs_single(
                pn, namespace, container, tail_lines, since_seconds,
                since_time, previous, timestamps,
            ))
        except (LookupError, ValueError) as e:
            # Skip pods that errored but keep going
            logger.warning("logs: skipping pod %s: %s", pn, e)
            records.append({
                "pod": pn, "container": container or "",
                "time": "", "line": f"[error: {e}]",
            })
    return records


def _parse_lines(text: str, *, pod: str, container: str) -> list[dict[str, str]]:
    """Parse raw log text into records. If timestamps are present (we set
    timestamps=True), each line starts with an RFC3339 timestamp."""
    if not text:
        return []
    out: list[dict[str, str]] = []
    for raw in text.splitlines():
        line = raw
        ts = ""
        # K8s default timestamp format: 2024-01-01T00:00:00.123456789Z message
        if line and line[0:1].isdigit() and "T" in line[:30]:
            space = line.find(" ")
            if space > 0 and space < 35:
                ts = line[:space]
                line = line[space + 1 :]
        out.append({"pod": pod, "container": container, "time": ts, "line": line})
    return out


def _filter_with_context(
    records: list[dict[str, str]], pattern: str, context_lines: int,
) -> list[dict[str, str]]:
    rx = re.compile(pattern)
    if context_lines <= 0:
        return [r for r in records if rx.search(r["line"])]
    keep: set[int] = set()
    for i, r in enumerate(records):
        if rx.search(r["line"]):
            lo = max(0, i - context_lines)
            hi = min(len(records), i + context_lines + 1)
            keep.update(range(lo, hi))
    return [records[i] for i in sorted(keep)]


def _parse_rfc3339(value: str, *, field: str) -> datetime:
    """Parse an RFC3339 timestamp into an aware `datetime` in UTC.

    Accepts:
      - 2026-07-02T14:00:00Z
      - 2026-07-02T14:00:00+08:00
      - 2026-07-02T14:00:00.123456Z
      - 2026-07-02T14:00:00.123456789Z
      - 2026-07-02 14:00:00 (with or without offset)

    Raises ValueError with a helpful message naming the field.
    """
    if not value:
        raise ValueError(f"{field} is empty")
    s = value.strip()
    # Tolerate the "YYYY-MM-DD HH:MM:SS" form (no T separator).
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    # Apiserver accepts "Z" or "+HH:MM" / "-HH:MM"; fromisoformat pre-3.11
    # didn't accept "Z", so we normalize.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError) as e:
        raise ValueError(
            f"{field}={value!r} is not valid RFC3339. {_RFC3339_EXAMPLES}. "
            f"Underlying parse error: {e}"
        ) from e
    if dt.tzinfo is None:
        # Treat naive timestamps as UTC. Most agents pass "Z" explicitly,
        # so this branch is mostly defensive.
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return dt


def _record_dt(record: dict[str, str]) -> datetime | None:
    """Parse a record's `time` field to a UTC datetime, or None if absent/invalid.

    We tolerate a few common shapes that kubernetes/kubelet emit:
      - "2026-07-02T14:00:00Z"
      - "2026-07-02T14:00:00.123456789Z"
      - "2026-07-02T14:00:00.123456+08:00"
    Anything else returns None — the caller decides whether to keep or drop it.
    """
    ts = (record.get("time") or "").strip()
    if not ts:
        return None
    try:
        return _parse_rfc3339(ts, field="<record.timestamp>")
    except ValueError:
        return None


def _filter_by_time(
    records: list[dict[str, str]],
    since_dt: datetime | None,
    until_dt: datetime | None,
    strict_time: bool,
) -> list[dict[str, str]]:
    """Filter records to those within [since_dt, until_dt] (inclusive bounds).

    Records without a parseable timestamp:
      - strict_time=True: dropped (caller has opted in to "only time-stamped lines")
      - strict_time=False: kept (caller wants everything; bounds just don't apply)
    """
    if since_dt is None and until_dt is None:
        return list(records)
    out: list[dict[str, str]] = []
    for r in records:
        dt = _record_dt(r)
        if dt is None:
            if strict_time:
                continue
            out.append(r)
            continue
        if since_dt is not None and dt < since_dt:
            continue
        if until_dt is not None and dt > until_dt:
            continue
        out.append(r)
    return out


def _empty_time_window_message(
    namespace: str,
    pod_name: str | None,
    label_selector: str | None,
    since_time: str | None,
    until_time: str | None,
    strict_time: bool,
) -> str:
    """Helpful message when the time-window filter narrows records to zero."""
    target = (
        f"pod '{namespace}/{pod_name}'" if pod_name
        else f"pods matching label_selector in namespace '{namespace}'"
    )
    window = (
        f"[{since_time or '...'} → {until_time or '...'}]"
    )
    lines = [f"(no log lines from {target} within {window}."]
    lines.append(
        "Possible causes:"
    )
    lines.append(
        "  - the container writes to a file rather than stdout/stderr,"
    )
    lines.append(
        "  - the time window falls outside the pod's log retention / tail_lines,"
    )
    lines.append(
        "  - the timestamps we parse are in a different timezone than you expected,"
    )
    if not strict_time:
        lines.append(
            "  - some/all log lines lack RFC3339 timestamps; pass strict_time=True "
            "to drop them and confirm,"
        )
    lines.append(
        "  - try a wider window (e.g. extend since_time earlier and until_time later) "
        "to see what's available."
    )
    lines.append(")")
    return "\n".join(lines)


def _serialize(
    records: list[dict[str, str]], max_bytes: int, output_format: str,
) -> tuple[str, bool]:
    """Return (payload, truncated)."""
    if output_format == "json":
        return _serialize_json(records, max_bytes)
    return _serialize_text(records, max_bytes)


def _serialize_text(records: list[dict[str, str]], max_bytes: int) -> tuple[str, bool]:
    # Fast path: serialize as lines; if over budget, drop from the head
    lines = []
    for r in records:
        prefix = ""
        if r["pod"] and r["container"]:
            prefix = f"[{r['pod']}/{r['container']}] "
        elif r["pod"]:
            prefix = f"[{r['pod']}] "
        ts = f"{r['time']} " if r["time"] else ""
        lines.append(f"{prefix}{ts}{r['line']}")
    body = "\n".join(lines)
    if len(body.encode("utf-8")) <= max_bytes:
        return body, False
    # Truncate from the head, keeping the tail (most recent)
    enc = body.encode("utf-8")
    kept = enc[-max_bytes:]
    # Trim to a line boundary
    nl = kept.find(b"\n")
    if 0 < nl < len(kept) - 1:
        kept = kept[nl + 1 :]
    return kept.decode("utf-8", errors="replace"), True


def _serialize_json(records: list[dict[str, str]], max_bytes: int) -> tuple[str, bool]:
    payload = json.dumps(records, ensure_ascii=False, indent=None)
    if len(payload.encode("utf-8")) <= max_bytes:
        return payload, False
    # Drop from the head (oldest)
    while records and len(payload.encode("utf-8")) > max_bytes:
        records.pop(0)
        payload = json.dumps(records, ensure_ascii=False, indent=None)
    return payload, True


def _list_containers(pod_name: str, namespace: str) -> list[str]:
    try:
        pod = _core_v1().read_namespaced_pod(name=pod_name, namespace=namespace)
        return [c.name for c in (pod.spec.containers or [])]
    except ApiException:
        return []


def register(mcp) -> None:
    mcp.tool()(get_pod_logs)
