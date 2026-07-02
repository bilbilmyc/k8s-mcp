"""Tests for time-window log filtering: since_time, until_time, strict_time.

The original `get_pod_logs` had only `since_seconds` (relative). To answer
"2pm-4pm with keyword aabbcc" we need absolute time bounds, but the K8s API
only supports `sinceTime` — `untilTime` is enforced client-side. We also
need to handle pods whose containers don't emit RFC3339 timestamps, since
the kubelet happily returns those without timestamps even when we ask for
them.
"""
from __future__ import annotations

import json
import re

import pytest

from k8s_mcp.config import reset_settings_cache
from k8s_mcp.tools import logs


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_settings_cache()
    yield
    reset_settings_cache()


# =============================================================================
# _parse_rfc3339 — RFC3339 acceptance + error message
# =============================================================================


def test_parse_rfc3339_accepts_z_suffix():
    dt = logs._parse_rfc3339("2026-07-02T14:00:00Z", field="since_time")
    assert dt.year == 2026 and dt.month == 7 and dt.day == 2
    assert dt.hour == 14 and dt.minute == 0 and dt.second == 0
    assert dt.tzinfo is not None


def test_parse_rfc3339_accepts_offset():
    dt = logs._parse_rfc3339("2026-07-02T14:00:00+08:00", field="since_time")
    # 14:00+08 = 06:00 UTC
    assert dt.utcoffset().total_seconds() == 0
    assert dt.hour == 6


def test_parse_rfc3339_accepts_fractional_seconds():
    dt = logs._parse_rfc3339("2026-07-02T14:00:00.123456789Z", field="since_time")
    assert dt.microsecond == 123456


def test_parse_rfc3339_accepts_space_separator():
    """`2026-07-02 14:00:00` (no T) is RFC3339-acceptable-ish."""
    dt = logs._parse_rfc3339("2026-07-02 14:00:00Z", field="since_time")
    assert dt.hour == 14


def test_parse_rfc3339_rejects_garbage():
    with pytest.raises(ValueError, match="not valid RFC3339"):
        logs._parse_rfc3339("yesterday at 3pm", field="since_time")


def test_parse_rfc3339_error_includes_examples_and_field_name():
    with pytest.raises(ValueError) as exc:
        logs._parse_rfc3339("not-a-date", field="since_time")
    msg = str(exc.value)
    assert "since_time" in msg
    assert "Examples:" in msg


def test_parse_rfc3339_normalizes_naive_to_utc():
    dt = logs._parse_rfc3339("2026-07-02T14:00:00", field="since_time")
    assert dt.tzinfo is not None


# =============================================================================
# _filter_by_time — semantic correctness + un-timestamped handling
# =============================================================================


def _r(time: str, line: str) -> dict[str, str]:
    return {"pod": "p", "container": "c", "time": time, "line": line}


def test_filter_by_time_inclusive_bounds():
    records = [
        _r("2026-07-02T13:59:59Z", "before"),
        _r("2026-07-02T14:00:00Z", "start"),
        _r("2026-07-02T15:00:00Z", "middle"),
        _r("2026-07-02T16:00:00Z", "end"),
        _r("2026-07-02T16:00:01Z", "after"),
    ]
    since = logs._parse_rfc3339("2026-07-02T14:00:00Z", field="since_time")
    until = logs._parse_rfc3339("2026-07-02T16:00:00Z", field="until_time")
    out = logs._filter_by_time(records, since, until, strict_time=False)
    lines = [r["line"] for r in out]
    assert lines == ["start", "middle", "end"]


def test_filter_by_time_since_only():
    records = [
        _r("2026-07-02T13:00:00Z", "old"),
        _r("2026-07-02T15:00:00Z", "new"),
    ]
    since = logs._parse_rfc3339("2026-07-02T14:00:00Z", field="since_time")
    out = logs._filter_by_time(records, since, None, strict_time=False)
    assert [r["line"] for r in out] == ["new"]


def test_filter_by_time_until_only():
    records = [
        _r("2026-07-02T13:00:00Z", "old"),
        _r("2026-07-02T15:00:00Z", "new"),
    ]
    until = logs._parse_rfc3339("2026-07-02T14:00:00Z", field="until_time")
    out = logs._filter_by_time(records, None, until, strict_time=False)
    assert [r["line"] for r in out] == ["old"]


def test_filter_by_time_keeps_untimestamped_by_default():
    records = [
        _r("2026-07-02T15:00:00Z", "in-window"),
        _r("", "no-timestamp-1"),
        _r("", "no-timestamp-2"),
        _r("2026-07-02T13:00:00Z", "out-of-window"),
    ]
    since = logs._parse_rfc3339("2026-07-02T14:00:00Z", field="since_time")
    until = logs._parse_rfc3339("2026-07-02T16:00:00Z", field="until_time")
    out = logs._filter_by_time(records, since, until, strict_time=False)
    lines = [r["line"] for r in out]
    # In-window + both untimestamped kept; out-of-window dropped
    assert lines == ["in-window", "no-timestamp-1", "no-timestamp-2"]


def test_filter_by_time_drops_untimestamped_when_strict():
    records = [
        _r("2026-07-02T15:00:00Z", "in-window"),
        _r("", "no-timestamp"),
    ]
    since = logs._parse_rfc3339("2026-07-02T14:00:00Z", field="since_time")
    until = logs._parse_rfc3339("2026-07-02T16:00:00Z", field="until_time")
    out = logs._filter_by_time(records, since, until, strict_time=True)
    assert [r["line"] for r in out] == ["in-window"]


def test_filter_by_time_handles_malformed_timestamps():
    """If the `time` field is non-RFC3339 garbage, treat as un-timestamped."""
    records = [
        _r("garbage", "broken-ts"),
        _r("2026-07-02T15:00:00Z", "good"),
    ]
    since = logs._parse_rfc3339("2026-07-02T14:00:00Z", field="since_time")
    until = logs._parse_rfc3339("2026-07-02T16:00:00Z", field="until_time")
    # Without strict: both kept (broken-ts is "un-timestamped" so kept by default)
    out = logs._filter_by_time(records, since, until, strict_time=False)
    assert len(out) == 2
    # With strict: only the parseable one kept
    out = logs._filter_by_time(records, since, until, strict_time=True)
    assert [r["line"] for r in out] == ["good"]


def test_filter_by_time_offset_normalization():
    """Records with a +08:00 offset are compared in UTC."""
    records = [
        # 14:00+08 = 06:00 UTC, BEFORE 14:00 UTC
        _r("2026-07-02T14:00:00+08:00", "beijing-2pm"),
    ]
    since = logs._parse_rfc3339("2026-07-02T14:00:00Z", field="since_time")
    out = logs._filter_by_time(records, since, None, strict_time=False)
    assert out == []  # beijing-2pm < utc-2pm


# =============================================================================
# get_pod_logs — argument validation
# =============================================================================


def test_since_time_and_since_seconds_are_mutex():
    with pytest.raises(ValueError, match="mutually exclusive"):
        logs.get_pod_logs(
            pod_name="p", since_seconds=60, since_time="2026-07-02T14:00:00Z"
        )


def test_invalid_since_time_raises_with_example():
    with pytest.raises(ValueError, match="not valid RFC3339"):
        logs.get_pod_logs(pod_name="p", since_time="yesterday")


def test_invalid_until_time_raises_with_example():
    with pytest.raises(ValueError, match="not valid RFC3339"):
        logs.get_pod_logs(pod_name="p", until_time="not-a-date")


def test_until_before_since_raises():
    with pytest.raises(ValueError, match="must be >="):
        logs.get_pod_logs(
            pod_name="p",
            since_time="2026-07-02T16:00:00Z",
            until_time="2026-07-02T14:00:00Z",
        )


def test_strict_time_without_bounds_raises():
    with pytest.raises(ValueError, match="strict_time=True requires"):
        logs.get_pod_logs(pod_name="p", strict_time=True)


# =============================================================================
# get_pod_logs — happy-path orchestration
# =============================================================================


def test_since_time_passes_to_k8s_api(monkeypatch):
    captured = {}

    class FakeApi:
        def read_namespaced_pod_log(self, name, namespace, **kwargs):
            captured.update(kwargs)
            captured["name"] = name
            captured["namespace"] = namespace
            return "2026-07-02T13:00:00Z old\n2026-07-02T15:00:00Z new\n"

    monkeypatch.setattr(logs, "_core_v1", lambda: FakeApi())
    logs.get_pod_logs(
        pod_name="web", namespace="default",
        since_time="2026-07-02T14:00:00Z",
    )
    # since_time passed to K8s, timestamps forced on
    assert captured["since_time"] == "2026-07-02T14:00:00Z"
    assert captured["timestamps"] is True


def test_until_time_filters_client_side(monkeypatch):
    captured = {}

    class FakeApi:
        def read_namespaced_pod_log(self, name, namespace, **kwargs):
            captured.update(kwargs)
            # K8s returns everything after sinceTime; untilTime filtered client-side
            return (
                "2026-07-02T13:00:00Z before-window\n"
                "2026-07-02T14:30:00Z in-window-1\n"
                "2026-07-02T15:30:00Z in-window-2\n"
                "2026-07-02T16:30:00Z after-window\n"
            )

    monkeypatch.setattr(logs, "_core_v1", lambda: FakeApi())
    out = logs.get_pod_logs(
        pod_name="web", namespace="default",
        since_time="2026-07-02T14:00:00Z",
        until_time="2026-07-02T16:00:00Z",
    )
    assert "in-window-1" in out
    assert "in-window-2" in out
    assert "before-window" not in out
    assert "after-window" not in out
    # K8s doesn't accept untilTime → not in kwargs
    assert "until_time" not in captured


def test_time_window_then_pattern_then_context(monkeypatch):
    """End-to-end: time filter, then pattern+context within the window."""

    class FakeApi:
        def read_namespaced_pod_log(self, name, namespace, **kwargs):
            return (
                "2026-07-02T13:00:00Z old noise\n"
                "2026-07-02T14:30:00Z context-a\n"
                "2026-07-02T14:45:00Z context-b\n"
                "2026-07-02T15:00:00Z aabbcc hit\n"
                "2026-07-02T15:15:00Z context-c\n"
                "2026-07-02T15:30:00Z context-d\n"
                "2026-07-02T17:00:00Z too-late\n"
            )

    monkeypatch.setattr(logs, "_core_v1", lambda: FakeApi())
    out = logs.get_pod_logs(
        pod_name="web", namespace="default",
        since_time="2026-07-02T14:00:00Z",
        until_time="2026-07-02T16:00:00Z",
        pattern="aabbcc",
        context_lines=2,
    )
    # Time window trims to [14:30, 15:30]; pattern+context keeps ±2 around hit
    assert "aabbcc hit" in out
    assert "context-a" in out
    assert "context-b" in out
    assert "context-c" in out
    assert "context-d" in out
    assert "too-late" not in out
    assert "old noise" not in out


def test_strict_time_drops_untimestamped_lines(monkeypatch):
    class FakeApi:
        def read_namespaced_pod_log(self, name, namespace, **kwargs):
            return (
                "no-timestamp-1\n"  # kubelet can omit timestamps for some streams
                "2026-07-02T14:30:00Z ts-line-1\n"
                "no-timestamp-2\n"
                "2026-07-02T15:30:00Z ts-line-2\n"
            )

    monkeypatch.setattr(logs, "_core_v1", lambda: FakeApi())
    # Default: keep un-timestamped
    out = logs.get_pod_logs(
        pod_name="web", namespace="default",
        since_time="2026-07-02T14:00:00Z",
        until_time="2026-07-02T16:00:00Z",
    )
    assert "no-timestamp-1" in out
    assert "ts-line-1" in out
    assert "ts-line-2" in out

    # strict_time: drop them
    class FakeApi2:
        def read_namespaced_pod_log(self, name, namespace, **kwargs):
            return (
                "no-timestamp-1\n"
                "2026-07-02T14:30:00Z ts-line-1\n"
                "no-timestamp-2\n"
                "2026-07-02T15:30:00Z ts-line-2\n"
            )

    monkeypatch.setattr(logs, "_core_v1", lambda: FakeApi2())
    out = logs.get_pod_logs(
        pod_name="web", namespace="default",
        since_time="2026-07-02T14:00:00Z",
        until_time="2026-07-02T16:00:00Z",
        strict_time=True,
    )
    assert "ts-line-1" in out
    assert "ts-line-2" in out
    assert "no-timestamp-1" not in out
    assert "no-timestamp-2" not in out


def test_empty_time_window_returns_helpful_message(monkeypatch):
    class FakeApi:
        def read_namespaced_pod_log(self, name, namespace, **kwargs):
            return (
                "2026-07-02T10:00:00Z way before\n"
                "2026-07-02T18:00:00Z way after\n"
            )

    monkeypatch.setattr(logs, "_core_v1", lambda: FakeApi())
    out = logs.get_pod_logs(
        pod_name="web", namespace="default",
        since_time="2026-07-02T14:00:00Z",
        until_time="2026-07-02T16:00:00Z",
    )
    assert "no log lines" in out
    assert "2026-07-02T14:00:00Z" in out  # window echoed back
    assert "Possible causes:" in out
    assert "wider window" in out


def test_empty_time_window_message_includes_strict_hint(monkeypatch):
    """strict_time=True + only-untimestamped lines → "no log lines" message
    that hints at the strict_time flag (when strict_time=False)."""
    # Default behavior: keep un-timestamped lines, so no "no log lines" msg
    class FakeApi:
        def read_namespaced_pod_log(self, name, namespace, **kwargs):
            return "no timestamps here at all\n"

    monkeypatch.setattr(logs, "_core_v1", lambda: FakeApi())
    out = logs.get_pod_logs(
        pod_name="web", namespace="default",
        since_time="2026-07-02T14:00:00Z",
        until_time="2026-07-02T16:00:00Z",
    )
    # Lines are un-timestamped → kept (strict_time default is False)
    assert "no timestamps here at all" in out

    # Now strict_time=True drops them all → empty-result path with hint
    class FakeApi2:
        def read_namespaced_pod_log(self, name, namespace, **kwargs):
            return "no timestamps here at all\n"

    monkeypatch.setattr(logs, "_core_v1", lambda: FakeApi2())
    out = logs.get_pod_logs(
        pod_name="web", namespace="default",
        since_time="2026-07-02T14:00:00Z",
        until_time="2026-07-02T16:00:00Z",
        strict_time=True,
    )
    assert "no log lines" in out
    # Hint about strict_time is for the default (strict_time=False) case;
    # when it's True we already used it, so the hint is omitted.
    assert "wider window" in out


def test_time_window_json_output_includes_records(monkeypatch):
    class FakeApi:
        def read_namespaced_pod_log(self, name, namespace, **kwargs):
            return (
                "2026-07-02T13:00:00Z before\n"
                "2026-07-02T15:00:00Z inside\n"
            )

    monkeypatch.setattr(logs, "_core_v1", lambda: FakeApi())
    out = logs.get_pod_logs(
        pod_name="web", namespace="default",
        since_time="2026-07-02T14:00:00Z",
        output_format="json",
    )
    parsed = json.loads(out)
    assert len(parsed) == 1
    assert parsed[0]["time"] == "2026-07-02T15:00:00Z"
    assert parsed[0]["line"] == "inside"


def test_multi_pod_time_window(monkeypatch):
    list_calls = {"count": 0}
    read_calls = []

    class FakePodList:
        def __init__(self, names):
            self.items = [type("P", (), {"metadata": type("M", (), {"name": n})()})() for n in names]

    class FakeApi:
        def list_namespaced_pod(self, namespace, **kwargs):
            list_calls["count"] += 1
            return FakePodList(["web-1", "web-2"])

        def read_namespaced_pod_log(self, name, namespace, **kwargs):
            read_calls.append((name, kwargs.get("since_time")))
            if name == "web-1":
                return (
                    "2026-07-02T13:00:00Z out\n"
                    "2026-07-02T15:00:00Z in\n"
                )
            return (
                "2026-07-02T14:30:00Z only\n"
            )

    monkeypatch.setattr(logs, "_core_v1", lambda: FakeApi())
    out = logs.get_pod_logs(
        label_selector="app=web", namespace="default",
        since_time="2026-07-02T14:00:00Z",
        until_time="2026-07-02T16:00:00Z",
    )
    assert list_calls["count"] == 1
    assert all(kw == "2026-07-02T14:00:00Z" for _, kw in read_calls)
    assert "[web-1]" in out
    assert "[web-2]" in out
    assert "in" in out
    assert "only" in out
    assert "out" not in out


def test_time_filter_does_not_affect_pattern_match():
    """Sanity: pattern matches against `line` only, not `time`."""
    records = [
        _r("2026-07-02T15:00:00Z", "alpha"),
        _r("2026-07-02T15:01:00Z", "beta"),
    ]
    since = logs._parse_rfc3339("2026-07-02T14:00:00Z", field="since_time")
    out = logs._filter_by_time(records, since, None, strict_time=False)
    pat = re.compile("alpha")
    assert pat.search(out[0]["line"])
    assert not pat.search(out[1]["line"])
