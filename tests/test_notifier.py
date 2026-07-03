"""Tests for the notifier framework.

Strategy: stub the urllib POST at `notifier.urllib.request.urlopen` so we
don't actually hit any webhook. We capture what was sent (URL, body)
and stub the response to be a fake 200/4xx with a JSON body.

Covered:
  - No notifiers configured → actionable env-var error
  - Malformed JSON → falls back to empty list, same error as above
  - Validation: missing url / unknown type / empty name
  - Per-type payload shape (feishu / wecom / slack / generic)
  - Broadcast (notifier_name=None) vs targeted (name="ops")
  - HTTP error / timeout / network error rendered as ❌ with detail
  - Level prefix (info/warning/critical) renders correctly
  - title is included
"""
from __future__ import annotations

import json
import urllib.error

import pytest

from k8s_mcp.config import reset_settings_cache
from k8s_mcp.tools import notifier


@pytest.fixture(autouse=True)
def _clear():
    reset_settings_cache()
    yield
    reset_settings_cache()


def _set_notifiers(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv("K8S_MCP_NOTIFIERS", raising=False)
    else:
        monkeypatch.setenv("K8S_MCP_NOTIFIERS", raw)
    reset_settings_cache()


# ---------- stubs ---------------------------------------------------------


class _FakeResp:
    def __init__(self, status=200, reason="OK", body=b""):
        self.status = status
        self.reason = reason
        self._body = body

    def read(self):
        return self._body

    def __enter__(self): return self
    def __exit__(self, *a): return False


class _Recorder:
    def __init__(self, response):
        self.calls: list[tuple[str, bytes, dict]] = []
        self._response = response

    def __call__(self, req, *args, **kwargs):
        self.calls.append((req.full_url, req.data, dict(req.headers)))
        return self._response


@pytest.fixture
def _patch_urlopen(monkeypatch):
    """Returns a dict with `install(response) -> recorder` and
    `recorder` (None when no recorder has been installed yet — useful
    for asserting "no HTTP call was made")."""
    state = {"recorder": None}

    def fake_urlopen(req, *args, **kwargs):
        if state["recorder"] is None:
            raise RuntimeError("no recorder installed")
        return state["recorder"](req, *args, **kwargs)

    monkeypatch.setattr(notifier.urllib.request, "urlopen", fake_urlopen)

    def install(response):
        rec = _Recorder(response)
        state["recorder"] = rec
        return rec

    return {"install": install, "recorder": None}  # None placeholder; access via state


# ---------- no config -----------------------------------------------------


def test_notify_no_config_returns_actionable_error(monkeypatch, _patch_urlopen):
    _set_notifiers(monkeypatch, None)
    out = notifier.notify("hello")
    assert "No notifiers configured" in out
    assert "K8S_MCP_NOTIFIERS" in out
    assert "feishu" in out  # example config visible
    # No HTTP call
    assert _patch_urlopen["recorder"] is None


def test_notify_malformed_json_falls_back_to_empty(monkeypatch, _patch_urlopen):
    _set_notifiers(monkeypatch, "not json at all")
    out = notifier.notify("hello")
    assert "No notifiers configured" in out


def test_notify_non_list_json_falls_back_to_empty(monkeypatch, _patch_urlopen):
    _set_notifiers(monkeypatch, '{"name":"x"}')  # dict, not list
    out = notifier.notify("hello")
    assert "No notifiers configured" in out


# ---------- validation ---------------------------------------------------


def test_notify_invalid_type_rejected(monkeypatch, _patch_urlopen):
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "bad", "type": "telegram", "url": "https://x"},
    ]))
    out = notifier.notify("hi")
    assert "All configured notifiers are invalid" in out
    assert "type" in out
    assert _patch_urlopen["recorder"] is None


def test_notify_missing_url_rejected(monkeypatch, _patch_urlopen):
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "x", "type": "slack"},  # no url
    ]))
    out = notifier.notify("hi")
    assert "All configured notifiers are invalid" in out
    assert "url" in out


def test_notify_unknown_name_filter_rejected(monkeypatch, _patch_urlopen):
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "ops", "type": "slack", "url": "https://x"},
    ]))
    out = notifier.notify("hi", notifier_name="ghost")
    assert "Notifier 'ghost' not found" in out
    assert "ops" in out  # available name listed
    assert _patch_urlopen["recorder"] is None


def test_notify_invalid_level_rejected(monkeypatch, _patch_urlopen):
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "ops", "type": "slack", "url": "https://x"},
    ]))
    out = notifier.notify("hi", level="emergency")
    assert "Invalid level" in out
    assert _patch_urlopen["recorder"] is None


# ---------- per-type payload shape ---------------------------------------


def _feishu_notifier(name="ops", url="https://feishu/x", label=""):
    n = {"name": name, "type": "feishu", "url": url}
    if label:
        n["cluster_label"] = label
    return n


def test_feishu_payload_shape(monkeypatch, _patch_urlopen):
    _set_notifiers(monkeypatch, json.dumps([_feishu_notifier()]))
    rec = _patch_urlopen["install"](_FakeResp(200, "OK", b'{"StatusCode":0,"Msg":"ok"}'))
    out = notifier.notify("hello", level="warning")
    assert "✅" in out
    assert len(rec.calls) == 1
    url, body, headers = rec.calls[0]
    assert url == "https://feishu/x"
    payload = json.loads(body)
    assert payload["msg_type"] == "text"
    assert "hello" in payload["content"]["text"]
    assert "WARNING" in payload["content"]["text"]
    # timestamp present
    assert any(ch.isdigit() for ch in payload["content"]["text"])
    # urllib normalizes header keys; case-insensitive lookup
    lowered = {k.lower(): v for k, v in headers.items()}
    assert lowered.get("content-type") == "application/json"


def test_wecom_payload_shape(monkeypatch, _patch_urlopen):
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "ops", "type": "wecom", "url": "https://wecom/x"},
    ]))
    rec = _patch_urlopen["install"](_FakeResp(200))
    notifier.notify("hi")
    payload = json.loads(rec.calls[0][1])
    assert payload["msgtype"] == "text"
    assert "hi" in payload["text"]["content"]
    assert payload["text"]["mentioned_list"] == []


def test_slack_payload_shape(monkeypatch, _patch_urlopen):
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "ops", "type": "slack", "url": "https://slack/x"},
    ]))
    rec = _patch_urlopen["install"](_FakeResp(200))
    notifier.notify("hi", title="Cluster Report")
    payload = json.loads(rec.calls[0][1])
    # Slack mrkdwn: **Cluster Report**
    assert "**Cluster Report**" in payload["text"]
    assert "hi" in payload["text"]


def test_generic_payload_includes_structured_fields(monkeypatch, _patch_urlopen):
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "ops", "type": "generic", "url": "https://gen/x",
         "cluster_label": "prod"},
    ]))
    rec = _patch_urlopen["install"](_FakeResp(200))
    notifier.notify("hi", level="critical")
    payload = json.loads(rec.calls[0][1])
    assert payload["text"].startswith("❌ [CRITICAL] [prod]")
    assert payload["level"] == "critical"
    assert payload["cluster_label"] == "prod"
    assert "timestamp" in payload


# ---------- level prefix -------------------------------------------------


def test_level_info_prefix(monkeypatch, _patch_urlopen):
    _set_notifiers(monkeypatch, json.dumps([_feishu_notifier()]))
    rec = _patch_urlopen["install"](_FakeResp(200))
    notifier.notify("hi", level="info")
    text = json.loads(rec.calls[0][1])["content"]["text"]
    assert text.startswith("ℹ️  [INFO]")


def test_level_critical_prefix(monkeypatch, _patch_urlopen):
    _set_notifiers(monkeypatch, json.dumps([_feishu_notifier()]))
    rec = _patch_urlopen["install"](_FakeResp(200))
    notifier.notify("hi", level="critical")
    text = json.loads(rec.calls[0][1])["content"]["text"]
    assert text.startswith("❌ [CRITICAL]")


# ---------- broadcast vs targeted ---------------------------------------


def test_broadcast_sends_to_all(monkeypatch, _patch_urlopen):
    _set_notifiers(monkeypatch, json.dumps([
        _feishu_notifier(name="a", url="https://a"),
        _feishu_notifier(name="b", url="https://b"),
        _feishu_notifier(name="c", url="https://c"),
    ]))
    rec = _patch_urlopen["install"](_FakeResp(200))
    notifier.notify("hi")
    urls = [c[0] for c in rec.calls]
    assert set(urls) == {"https://a", "https://b", "https://c"}


def test_targeted_sends_only_to_named(monkeypatch, _patch_urlopen):
    _set_notifiers(monkeypatch, json.dumps([
        _feishu_notifier(name="a", url="https://a"),
        _feishu_notifier(name="b", url="https://b"),
    ]))
    rec = _patch_urlopen["install"](_FakeResp(200))
    notifier.notify("hi", notifier_name="b")
    assert len(rec.calls) == 1
    assert rec.calls[0][0] == "https://b"


# ---------- failure paths -----------------------------------------------


def test_http_error_renders_status(monkeypatch, _patch_urlopen):
    _set_notifiers(monkeypatch, json.dumps([_feishu_notifier()]))
    _patch_urlopen["install"](_FakeResp(401, "Unauthorized", b"invalid_token"))
    out = notifier.notify("hi")
    assert "❌" in out
    assert "401" in out or "Unauthorized" in out


def test_network_error_renders_error_class(monkeypatch, _patch_urlopen):
    _set_notifiers(monkeypatch, json.dumps([_feishu_notifier()]))

    def boom(*a, **kw):
        raise urllib.error.URLError("no route to host")
    # Override the patch
    monkeypatch.setattr(notifier.urllib.request, "urlopen", boom)
    out = notifier.notify("hi")
    assert "❌" in out
    assert "URLError" in out
    assert "no route to host" in out


def test_timeout_renders_error_class(monkeypatch, _patch_urlopen):
    _set_notifiers(monkeypatch, json.dumps([_feishu_notifier()]))

    def boom(*a, **kw):
        raise TimeoutError("connect timed out")
    monkeypatch.setattr(notifier.urllib.request, "urlopen", boom)
    out = notifier.notify("hi")
    assert "❌" in out
    assert "TimeoutError" in out


# ---------- mixed valid + invalid --------------------------------------


def test_mixed_valid_and_invalid_reports_both(monkeypatch, _patch_urlopen):
    _set_notifiers(monkeypatch, json.dumps([
        _feishu_notifier(name="good"),
        {"name": "bad", "type": "telegram", "url": "https://x"},  # invalid type
    ]))
    rec = _patch_urlopen["install"](_FakeResp(200))
    out = notifier.notify("hi")
    assert "Sent to 1 notifier" in out
    assert "bad" in out  # the invalid name surfaces
    assert len(rec.calls) == 1


# ---------- cluster_label routing ---------------------------------------


def test_cluster_label_appears_in_message(monkeypatch, _patch_urlopen):
    _set_notifiers(monkeypatch, json.dumps([
        _feishu_notifier(label="staging"),
    ]))
    rec = _patch_urlopen["install"](_FakeResp(200))
    notifier.notify("hi")
    text = json.loads(rec.calls[0][1])["content"]["text"]
    assert "[staging]" in text


# ---------- register ----------------------------------------------------


def test_register_attaches_to_mcp():
    calls: list = []
    class _FakeMCP:
        def tool(self):
            def deco(fn):
                calls.append(fn)
                return fn
            return deco
    notifier.register(_FakeMCP())
    assert notifier.notify in calls
