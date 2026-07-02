"""Tests for the empty-log UX fix.

When get_pod_logs returns 0 records, we surface a helpful notice instead
of an empty string (which many MCP clients hide, making it look like the
tool wasn't called).
"""
from __future__ import annotations

import pytest

from k8s_mcp.config import reset_settings_cache
from k8s_mcp.tools import logs


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_settings_cache()
    yield
    reset_settings_cache()


def test_empty_log_returns_informative_message_single_pod(monkeypatch):
    class FakeApi:
        def read_namespaced_pod_log(self, name, namespace, **kwargs):
            return ""

    monkeypatch.setattr(logs, "_core_v1", lambda: FakeApi())
    out = logs.get_pod_logs(pod_name="web-1", namespace="default")
    assert "no log lines" in out
    assert "pod 'default/web-1'" in out
    # Must not be empty (we want clients to actually display this)
    assert out.strip() != ""


def test_empty_log_returns_informative_message_multi_pod(monkeypatch):
    """When label_selector matches but no pods have logs, surface info."""
    pods_list = []

    class FakePodList:
        def __init__(self, items):
            self.items = items

    class FakeApi:
        def list_namespaced_pod(self, namespace, **kwargs):
            return FakePodList(pods_list)

        def read_namespaced_pod_log(self, name, namespace, **kwargs):
            return ""

    monkeypatch.setattr(logs, "_core_v1", lambda: FakeApi())
    # No pods match → empty result
    out = logs.get_pod_logs(label_selector="app=nope", namespace="default")
    # Empty list → still informative
    assert "no log lines" in out or "no pods" in out
    assert out.strip() != ""


def test_log_with_content_not_shadowed(monkeypatch):
    class FakeApi:
        def read_namespaced_pod_log(self, name, namespace, **kwargs):
            return "2026-01-01T00:00:00.000Z hello world\n"

    monkeypatch.setattr(logs, "_core_v1", lambda: FakeApi())
    out = logs.get_pod_logs(pod_name="p1", namespace="default")
    assert "hello world" in out
    assert "no log lines" not in out
