"""Tests for the new get_pod_logs filtering, context, multi-pod, and size cap."""
from __future__ import annotations

import pytest

from k8s_mcp.config import reset_settings_cache
from k8s_mcp.tools import logs


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_settings_cache()
    yield
    reset_settings_cache()


# ---- direct unit tests on internals ----------------------------------------


def test_filter_with_context_no_pattern_returns_all():
    records = [{"pod": "p", "container": "c", "time": "", "line": x} for x in ["a", "b", "c"]]
    assert logs._filter_with_context(records, "", 0) == records


def test_filter_with_context_basic():
    records = [{"pod": "p", "container": "c", "time": "", "line": x}
               for x in ["INFO start", "ERROR boom", "INFO end"]]
    out = logs._filter_with_context(records, "ERROR", 0)
    assert len(out) == 1
    assert "ERROR boom" in out[0]["line"]


def test_filter_with_context_includes_surrounding():
    records = [{"pod": "p", "container": "c", "time": "", "line": x}
               for x in ["line0", "line1", "ERROR here", "line3", "line4"]]
    # context_lines=2 around index 2 → range [0, 5) → all 5 lines
    out = logs._filter_with_context(records, "ERROR", context_lines=2)
    lines = [r["line"] for r in out]
    assert lines == ["line0", "line1", "ERROR here", "line3", "line4"]


def test_filter_with_context_clamps_at_boundaries():
    records = [{"pod": "p", "container": "c", "time": "", "line": x}
               for x in ["ERROR first", "middle", "tail"]]
    # context_lines=5 around index 0 → range [0, 3) → all 3 (no negative)
    out = logs._filter_with_context(records, "ERROR", context_lines=5)
    lines = [r["line"] for r in out]
    assert lines == ["ERROR first", "middle", "tail"]


def test_filter_with_context_dedupes_overlapping_ranges():
    records = [{"pod": "p", "container": "c", "time": "", "line": x}
               for x in ["a", "ERROR x", "ERROR y", "d"]]
    out = logs._filter_with_context(records, "ERROR", context_lines=1)
    lines = [r["line"] for r in out]
    assert lines == ["a", "ERROR x", "ERROR y", "d"]


def test_filter_with_context_regex():
    records = [{"pod": "p", "container": "c", "time": "", "line": x}
               for x in ["200 GET /", "500 POST /x", "200 GET /y"]]
    out = logs._filter_with_context(records, r"^5\d\d", 0)
    assert len(out) == 1
    assert "500 POST" in out[0]["line"]


def test_parse_lines_no_timestamps():
    records = logs._parse_lines("a\nb\nc", pod="p", container="c")
    assert [r["line"] for r in records] == ["a", "b", "c"]
    assert all(r["time"] == "" for r in records)


def test_parse_lines_with_timestamps():
    text = "2026-01-01T00:00:00.000Z hello\n2026-01-01T00:00:01.000Z world"
    records = logs._parse_lines(text, pod="p", container="c")
    assert records[0]["time"] == "2026-01-01T00:00:00.000Z"
    assert records[0]["line"] == "hello"
    assert records[1]["time"] == "2026-01-01T00:00:01.000Z"
    assert records[1]["line"] == "world"


def test_serialize_text_under_limit():
    records = [{"pod": "p", "container": "", "time": "", "line": "hi"}]
    out, trunc = logs._serialize_text(records, max_bytes=1000)
    assert not trunc
    assert "[p]" in out


def test_serialize_text_truncates_from_head():
    records = [{"pod": "p", "container": "", "time": "", "line": f"line{i:04d}"} for i in range(1000)]
    out, trunc = logs._serialize_text(records, max_bytes=200)
    assert trunc is True
    # Should keep tail, drop head
    assert "line0999" in out
    assert "line0000" not in out


def test_serialize_json_under_limit():
    records = [{"pod": "p", "container": "c", "time": "", "line": "x"}]
    out, trunc = logs._serialize_json(records, max_bytes=1000)
    assert not trunc
    import json
    assert json.loads(out) == records


def test_serialize_json_truncates():
    records = [{"pod": "p", "container": "c", "time": "", "line": f"x{i}"} for i in range(1000)]
    out, trunc = logs._serialize_json(records, max_bytes=500)
    assert trunc is True
    import json
    parsed = json.loads(out)
    assert len(parsed) < 1000
    # keeps tail
    assert parsed[-1]["line"] == "x999"


# ---- integration: argument validation and orchestration ---------------------


def test_mutually_exclusive_pod_and_label():
    with pytest.raises(ValueError, match="mutually exclusive"):
        logs.get_pod_logs(pod_name="x", label_selector="app=x")


def test_one_of_pod_or_label_required():
    with pytest.raises(ValueError, match="required"):
        logs.get_pod_logs()


def test_max_bytes_cap():
    with pytest.raises(ValueError, match="max_bytes may not exceed"):
        logs.get_pod_logs(pod_name="x", max_bytes=20_000_000)


def test_invalid_output_format():
    with pytest.raises(ValueError, match="output_format must be"):
        logs.get_pod_logs(pod_name="x", output_format="yaml")


def test_single_pod_with_filter_calls_core_v1(monkeypatch):
    """The happy-path single pod: fetch → filter → return."""
    captured = {}

    class FakeApi:
        def read_namespaced_pod_log(self, name, namespace, **kwargs):
            captured["name"] = name
            captured["namespace"] = namespace
            captured["kwargs"] = kwargs
            return "2026-01-01T00:00:00.000Z INFO a\n2026-01-01T00:00:01.000Z ERROR boom\n2026-01-01T00:00:02.000Z INFO c\n"

    monkeypatch.setattr(logs, "_core_v1", lambda: FakeApi())
    out = logs.get_pod_logs(
        pod_name="web-1", namespace="default", tail_lines=10, pattern="ERROR"
    )
    assert "ERROR boom" in out
    assert "INFO a" not in out
    assert captured["name"] == "web-1"
    assert captured["namespace"] == "default"


def test_single_pod_truncation_marker(monkeypatch):
    big_line = "x" * 500
    text = "\n".join([f"line{i:04d}-{big_line}" for i in range(100)])

    class FakeApi:
        def read_namespaced_pod_log(self, name, namespace, **kwargs):
            return text

    monkeypatch.setattr(logs, "_core_v1", lambda: FakeApi())
    out = logs.get_pod_logs(pod_name="web-1", namespace="default", max_bytes=2000)
    assert "[truncated" in out


def test_multi_pod_uses_list_then_per_pod(monkeypatch):
    """When label_selector is set, list pods first, then fetch each."""
    list_calls = {"count": 0}
    read_calls = []

    class FakePodList:
        def __init__(self, names):
            self.items = [type("P", (), {"metadata": type("M", (), {"name": n})()})() for n in names]

    class FakeApi:
        def list_namespaced_pod(self, namespace, **kwargs):
            list_calls["count"] += 1
            list_calls["selector"] = kwargs.get("label_selector")
            return FakePodList(["web-1", "web-2"])

        def read_namespaced_pod_log(self, name, namespace, **kwargs):
            read_calls.append(name)
            return f"2026-01-01T00:00:00.000Z hello from {name}\n"

    monkeypatch.setattr(logs, "_core_v1", lambda: FakeApi())
    out = logs.get_pod_logs(
        label_selector="app=web", namespace="default", tail_lines=10,
    )
    assert list_calls["count"] == 1
    assert list_calls["selector"] == "app=web"
    assert read_calls == ["web-1", "web-2"]
    assert "[web-1]" in out
    assert "[web-2]" in out


def test_multi_pod_continues_on_pod_error(monkeypatch):
    """If one pod errors, others still return logs."""

    class FakePodList:
        def __init__(self, names):
            self.items = [type("P", (), {"metadata": type("M", (), {"name": n})()})() for n in names]

    class FakeApi:
        def list_namespaced_pod(self, namespace, **kwargs):
            return FakePodList(["good", "bad"])

        def read_namespaced_pod_log(self, name, namespace, **kwargs):
            if name == "bad":
                from kubernetes.client.rest import ApiException
                raise ApiException(status=404, reason="not found")
            return f"2026-01-01T00:00:00.000Z ok {name}\n"

    monkeypatch.setattr(logs, "_core_v1", lambda: FakeApi())
    out = logs.get_pod_logs(label_selector="app=x", namespace="default")
    assert "ok good" in out
    assert "[bad]" in out
    assert "error" in out


def test_json_output_format(monkeypatch):
    class FakeApi:
        def read_namespaced_pod_log(self, name, namespace, **kwargs):
            return "2026-01-01T00:00:00.000Z hello\n"

    monkeypatch.setattr(logs, "_core_v1", lambda: FakeApi())
    out = logs.get_pod_logs(
        pod_name="p", namespace="default", output_format="json",
    )
    import json
    parsed = json.loads(out)
    assert parsed[0]["pod"] == "p"
    assert parsed[0]["line"] == "hello"
    assert parsed[0]["time"] == "2026-01-01T00:00:00.000Z"
