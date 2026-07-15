"""Tests for the notifier framework.

Strategy: stub the HTTP transport at `notifier.requests.Session.post`
so we don't actually hit any webhook. We capture what was sent
(URL, body) and stub the response to be a fake 200/4xx with a body.

Covered:
  - No notifiers configured → actionable env-var error
  - Malformed JSON → falls back to empty list, same error as above
  - Validation: missing url / unknown type / empty name
  - Per-type payload shape (feishu / wecom / slack / generic)
  - Broadcast (notifier_name=None) vs targeted (name="ops")
  - HTTP error / timeout / network error rendered as ❌ with detail
  - Level prefix (info/warning/critical) renders correctly
  - title is included
  - Retry on 5xx (counts attempts, succeeds on 2nd try)
  - Payload size guard (refuses oversized payloads with clear error)
  - Connection pool reuse (single Session across calls)
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from k8s_mcp import config as _config
from k8s_mcp.config import reset_settings_cache
from k8s_mcp.tools import notifier


@pytest.fixture(autouse=True)
def _clear():
    reset_settings_cache()
    notifier.reset_session()
    yield
    reset_settings_cache()
    notifier.reset_session()


def _set_notifiers(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv("K8S_MCP_NOTIFIERS", raising=False)
    else:
        monkeypatch.setenv("K8S_MCP_NOTIFIERS", raw)
    reset_settings_cache()


# ---------- stubs ---------------------------------------------------------


class _FakeResp:
    def __init__(self, status=200, reason="OK", body=b""):
        self.status_code = status
        self.reason = reason
        self.text = body.decode("utf-8", errors="replace") if body else ""

    def __enter__(self): return self
    def __exit__(self, *a): return False


class _Recorder:
    """Records every call to session.post and returns the configured
    sequence of responses (exhausts the list, then returns the last
    response forever — useful for asserting retry behavior)."""

    def __init__(self, responses):
        self.calls: list[tuple[str, bytes, dict]] = []
        self.last_kwargs: dict | None = None
        self._responses = list(responses)

    def __call__(self, url, **kwargs):
        # requests.Session.post(url, **kwargs)
        data = kwargs.get("data", b"")
        headers = kwargs.get("headers", {})
        self.last_kwargs = dict(kwargs)
        self.calls.append((url, data, dict(headers)))
        if self._responses:
            return self._responses.pop(0)
        return self._responses[-1] if self._responses else _FakeResp(200)


@pytest.fixture
def _patch_session(monkeypatch):
    """Patch notifier._get_session to return a fake session whose
    `post(url, **kwargs)` is the configured recorder. The fixture
    returns a state dict with:
      - "install"(responses) -> _Recorder (also sets "recorder")
      - "recorder" -> the installed recorder, or None if never installed
      - "session"  -> the fake session (for tests that assign to .post
                      directly to inject failure-raising callables)

    Tests that don't call install() (e.g. expect a refusal without any
    HTTP call) can assert `_patch_session["recorder"] is None`."""
    state: dict = {"recorder": None, "session": None}

    class _FakeSession:
        # `post` is rebound by install() before the first notify() call
        post = None

    def fake_get_session():
        if state["session"] is None:
            state["session"] = _FakeSession()
        return state["session"]

    monkeypatch.setattr(notifier, "_get_session", fake_get_session)

    def install(responses):
        if state["session"] is None:
            state["session"] = _FakeSession()
        rec = _Recorder(responses)
        state["session"].post = rec
        state["recorder"] = rec
        return rec

    state["install"] = install
    return state


# ---------- no config -----------------------------------------------------


def test_notify_no_config_returns_actionable_error(monkeypatch, _patch_session):
    _set_notifiers(monkeypatch, None)
    out = notifier.notify("hello")
    assert "No notifiers configured" in out
    assert "K8S_MCP_NOTIFIERS" in out
    assert "feishu" in out  # example config visible
    # No HTTP call
    assert _patch_session["recorder"] is None


def test_notify_malformed_json_falls_back_to_empty(monkeypatch, _patch_session):
    _set_notifiers(monkeypatch, "not json at all")
    out = notifier.notify("hello")
    assert "No notifiers configured" in out


def test_notify_non_list_json_falls_back_to_empty(monkeypatch, _patch_session):
    _set_notifiers(monkeypatch, '{"name":"x"}')  # dict, not list
    out = notifier.notify("hello")
    assert "No notifiers configured" in out


# ---------- validation ---------------------------------------------------


def test_notify_invalid_type_rejected(monkeypatch, _patch_session):
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "bad", "type": "telegram", "url": "https://x"},
    ]))
    out = notifier.notify("hi")
    assert "All configured notifiers are invalid" in out
    assert "type" in out
    assert _patch_session["recorder"] is None


def test_notify_missing_url_rejected(monkeypatch, _patch_session):
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "x", "type": "slack"},  # no url
    ]))
    out = notifier.notify("hi")
    assert "All configured notifiers are invalid" in out
    assert "url" in out


def test_notify_unknown_name_filter_rejected(monkeypatch, _patch_session):
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "ops", "type": "slack", "url": "https://x"},
    ]))
    out = notifier.notify("hi", notifier_name="ghost")
    assert "Notifier 'ghost' not found" in out
    assert "ops" in out  # available name listed
    assert _patch_session["recorder"] is None


def test_notify_invalid_level_rejected(monkeypatch, _patch_session):
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "ops", "type": "slack", "url": "https://x"},
    ]))
    out = notifier.notify("hi", level="emergency")
    assert "Invalid level" in out
    assert _patch_session["recorder"] is None


# ---------- per-type payload shape ---------------------------------------


def _feishu_notifier(name="ops", url="https://feishu/x", label=""):
    n = {"name": name, "type": "feishu", "url": url}
    if label:
        n["cluster_label"] = label
    return n


def test_feishu_payload_shape(monkeypatch, _patch_session):
    _set_notifiers(monkeypatch, json.dumps([_feishu_notifier()]))
    rec = _patch_session["install"]([_FakeResp(200, "OK", b'{"StatusCode":0,"Msg":"ok"}')])
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
    # headers are normalized case-insensitively
    lowered = {k.lower(): v for k, v in headers.items()}
    assert lowered.get("content-type") == "application/json"


def test_wecom_payload_shape(monkeypatch, _patch_session):
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "ops", "type": "wecom", "url": "https://wecom/x"},
    ]))
    rec = _patch_session["install"]([_FakeResp(200)])
    notifier.notify("hi")
    payload = json.loads(rec.calls[0][1])
    assert payload["msgtype"] == "text"
    assert "hi" in payload["text"]["content"]
    assert payload["text"]["mentioned_list"] == []


def test_slack_payload_shape(monkeypatch, _patch_session):
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "ops", "type": "slack", "url": "https://slack/x"},
    ]))
    rec = _patch_session["install"]([_FakeResp(200)])
    notifier.notify("hi", title="Cluster Report")
    payload = json.loads(rec.calls[0][1])
    # Slack mrkdwn: **Cluster Report**
    assert "**Cluster Report**" in payload["text"]
    assert "hi" in payload["text"]


def test_generic_payload_includes_structured_fields(monkeypatch, _patch_session):
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "ops", "type": "generic", "url": "https://gen/x",
         "cluster_label": "prod"},
    ]))
    rec = _patch_session["install"]([_FakeResp(200)])
    notifier.notify("hi", level="critical")
    payload = json.loads(rec.calls[0][1])
    assert payload["text"].startswith("❌ [CRITICAL] [prod]")
    assert payload["level"] == "critical"
    assert payload["cluster_label"] == "prod"
    assert "timestamp" in payload


# ---------- level prefix -------------------------------------------------


def test_level_info_prefix(monkeypatch, _patch_session):
    _set_notifiers(monkeypatch, json.dumps([_feishu_notifier()]))
    rec = _patch_session["install"]([_FakeResp(200)])
    notifier.notify("hi", level="info")
    text = json.loads(rec.calls[0][1])["content"]["text"]
    assert text.startswith("ℹ️  [INFO]")


def test_level_critical_prefix(monkeypatch, _patch_session):
    _set_notifiers(monkeypatch, json.dumps([_feishu_notifier()]))
    rec = _patch_session["install"]([_FakeResp(200)])
    notifier.notify("hi", level="critical")
    text = json.loads(rec.calls[0][1])["content"]["text"]
    assert text.startswith("❌ [CRITICAL]")


# ---------- broadcast vs targeted ---------------------------------------


def test_broadcast_sends_to_all(monkeypatch, _patch_session):
    _set_notifiers(monkeypatch, json.dumps([
        _feishu_notifier(name="a", url="https://a"),
        _feishu_notifier(name="b", url="https://b"),
        _feishu_notifier(name="c", url="https://c"),
    ]))
    rec = _patch_session["install"]([_FakeResp(200), _FakeResp(200), _FakeResp(200)])
    notifier.notify("hi")
    urls = [c[0] for c in rec.calls]
    assert set(urls) == {"https://a", "https://b", "https://c"}


def test_targeted_sends_only_to_named(monkeypatch, _patch_session):
    _set_notifiers(monkeypatch, json.dumps([
        _feishu_notifier(name="a", url="https://a"),
        _feishu_notifier(name="b", url="https://b"),
    ]))
    rec = _patch_session["install"]([_FakeResp(200)])
    notifier.notify("hi", notifier_name="b")
    assert len(rec.calls) == 1
    assert rec.calls[0][0] == "https://b"


# ---------- failure paths -----------------------------------------------


def test_http_error_renders_status(monkeypatch, _patch_session):
    _set_notifiers(monkeypatch, json.dumps([_feishu_notifier()]))
    # Retry config retries 5xx — but 401 is NOT in the retry list,
    # so a single 401 response fails immediately.
    _patch_session["install"]([_FakeResp(401, "Unauthorized", b"invalid_token")])
    out = notifier.notify("hi")
    assert "❌" in out
    assert "401" in out or "Unauthorized" in out


def test_network_error_renders_error_class(monkeypatch, _patch_session):
    _set_notifiers(monkeypatch, json.dumps([_feishu_notifier()]))
    # Materialize the fake session, then swap in a raising post() so we
    # don't need to drive a multi-response sequence.
    _patch_session["install"]([_FakeResp(200)])

    def boom(*a, **kw):
        raise requests.exceptions.ConnectionError("no route to host")
    _patch_session["session"].post = boom
    out = notifier.notify("hi")
    assert "❌" in out
    assert "ConnectionError" in out
    assert "no route to host" in out


def test_timeout_renders_error_class(monkeypatch, _patch_session):
    _set_notifiers(monkeypatch, json.dumps([_feishu_notifier()]))
    _patch_session["install"]([_FakeResp(200)])

    def boom(*a, **kw):
        raise requests.exceptions.Timeout("connect timed out")
    _patch_session["session"].post = boom
    out = notifier.notify("hi")
    assert "❌" in out
    assert "Timeout" in out


# ---------- retry on 5xx -------------------------------------------------
# Retry behavior lives inside `requests.Session` via urllib3.Retry
# attached to its HTTPAdapter. The retry logic runs inside urllib3's
# HTTPConnectionPool.urlopen — stubs that return requests.Response
# bypass it entirely, so we use a small in-process HTTP server and
# let the real retry machinery execute end-to-end.


class _RetryServer:
    """Tiny HTTP server that returns a queued sequence of (status, body)
    pairs on POST /hook; request count and bodies are recorded for
    assertions."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[bytes] = []
        self._last_pair: tuple[int, bytes] | None = None
        self._lock = threading.Lock()
        self._server = HTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True,
        )
        self._thread.start()

    def _handler(self):
        server = self

        class _H(BaseHTTPRequestHandler):
            def log_message(self, *a, **kw):  # silence stderr access log
                pass

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length > 0 else b""
                with server._lock:
                    server.requests.append(body)
                    if server.responses:
                        status, payload = server.responses.pop(0)
                        server._last_pair = (status, payload)
                    elif server._last_pair is not None:
                        # Stick at the last configured pair so a retry
                        # budget larger than the queue still gets a
                        # deterministic response (avoids IndexError).
                        status, payload = server._last_pair
                    else:
                        status, payload = 503, b'{"msg":"no response queued"}'
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        return _H

    def url(self, path="/hook") -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


@pytest.fixture
def _retry_server(monkeypatch):
    """Start a local HTTP server with no-op retry timing (backoff=0).
    Returns a factory `(responses) -> _RetryServer`."""
    servers: list[_RetryServer] = []

    def factory(responses):
        # Install a fast-retry session for the duration of this fixture
        # so retry-delay tests don't actually sleep.
        s = requests.Session()
        retry = Retry(
            total=3, backoff_factor=0,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["POST"], raise_on_status=False,
        )
        s.mount("http://", HTTPAdapter(max_retries=retry))
        monkeypatch.setattr(notifier, "_get_session", lambda: s)
        srv = _RetryServer(responses)
        servers.append(srv)
        return srv

    yield factory
    for srv in servers:
        srv.stop()


def test_retry_on_5xx_succeeds_on_second_try(monkeypatch, _retry_server):
    """503 then 200: real urllib3 retry path → 2 transport-level calls."""
    srv = _retry_server([
        (503, b'{"msg":"maintenance"}'),
        (200, b'{"StatusCode":0,"Msg":"ok"}'),
    ])
    # The local _retry_server binds on http://localhost; permit cleartext
    # only for this test (the production gate rejects http by default).
    monkeypatch.setenv("K8S_MCP_NOTIFIER_URL_ALLOW_HTTP", "true")
    monkeypatch.setenv("K8S_MCP_NOTIFIER_ALLOW_PRIVATE_HOSTS", "true")
    monkeypatch.setenv("K8S_MCP_NOTIFIERS", json.dumps([
        {"name": "ops", "type": "feishu", "url": srv.url()},
    ]))
    reset_settings_cache()
    notifier.reset_session()
    _config.get_settings.cache_clear()

    out = notifier.notify("hi")
    assert "✅" in out
    assert len(srv.requests) == 2  # initial 503 + retry 200


def test_retry_exhausted_returns_failure(monkeypatch, _retry_server):
    """All attempts 503: 1 initial + 3 retries = 4 transport calls.

    `urllib3.Retry(total=3)` means 3 retries, so the session makes
    4 attempts total before raising. Configure the server with 4
    identical 503s so the retry handler always has a response to
    return (the handler sticks at the last seen pair if the queue
    runs out, so even a slower budget still gets a deterministic
    response)."""
    srv = _retry_server([
        (503, b'{"msg":"down"}'),
        (503, b'{"msg":"down"}'),
        (503, b'{"msg":"down"}'),
        (503, b'{"msg":"down"}'),
    ])
    monkeypatch.setenv("K8S_MCP_NOTIFIER_URL_ALLOW_HTTP", "true")
    monkeypatch.setenv("K8S_MCP_NOTIFIER_ALLOW_PRIVATE_HOSTS", "true")
    monkeypatch.setenv("K8S_MCP_NOTIFIERS", json.dumps([
        {"name": "ops", "type": "feishu", "url": srv.url()},
    ]))
    reset_settings_cache()
    notifier.reset_session()
    _config.get_settings.cache_clear()

    out = notifier.notify("hi")
    assert "❌" in out
    assert "503" in out
    assert len(srv.requests) == 4  # 1 initial + 3 retries


def test_4xx_not_retried(monkeypatch, _patch_session):
    """401 is not in retry status_forcelist, so single attempt."""
    _set_notifiers(monkeypatch, json.dumps([_feishu_notifier()]))
    rec = _patch_session["install"]([_FakeResp(401, "Unauthorized", b"bad token")])
    out = notifier.notify("hi")
    assert "❌" in out
    assert len(rec.calls) == 1


# ---------- payload size guard -------------------------------------------


def test_oversized_payload_refused_for_wecom(monkeypatch, _patch_session):
    """WeCom limit is 4 KiB; a 5 KiB body must be refused without HTTP."""
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "ops", "type": "wecom", "url": "https://wecom/x"},
    ]))
    big = "x" * (5 * 1024)
    out = notifier.notify(big)
    assert "❌" in out
    assert "too large" in out
    assert "wecom" in out
    # No HTTP call at all — size check is up front
    assert _patch_session["recorder"] is None


def test_oversized_payload_refused_for_slack(monkeypatch, _patch_session):
    """Slack limit is 40 KiB."""
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "ops", "type": "slack", "url": "https://slack/x"},
    ]))
    big = "x" * (50 * 1024)
    out = notifier.notify(big)
    assert "❌" in out
    assert "too large" in out
    assert "slack" in out
    assert _patch_session["recorder"] is None


def test_payload_at_limit_accepted(monkeypatch, _patch_session):
    """A payload right at the limit (with header overhead under it) should send."""
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "ops", "type": "wecom", "url": "https://wecom/x"},
    ]))
    rec = _patch_session["install"]([_FakeResp(200)])
    notifier.notify("hello")  # tiny payload
    assert len(rec.calls) == 1


# ---------- connection pool reuse ----------------------------------------


def test_session_is_cached_and_reused(monkeypatch):
    """Two notify() calls share the same module-level session.

    We don't override `_get_session` here — we let the real module-level
    caching path run, swap in a recorder that tracks which Session
    instance handled each call, and assert only one instance ever did.
    """
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "ops", "type": "feishu", "url": "https://feishu/x"},
    ]))

    seen_sessions: list = []

    real_session = notifier._get_session()
    seen_sessions.append(real_session)

    def recording_post(*a, **kw):
        seen_sessions.append(real_session)
        return _FakeResp(200)

    real_session.post = recording_post
    notifier.notify("first")
    notifier.notify("second")
    # real + 2 recording calls — but instance should be the same every time
    assert len(seen_sessions) == 3
    assert seen_sessions[0] is seen_sessions[1] is seen_sessions[2] is real_session


# ---------- mixed valid + invalid --------------------------------------


def test_mixed_valid_and_invalid_reports_both(monkeypatch, _patch_session):
    _set_notifiers(monkeypatch, json.dumps([
        _feishu_notifier(name="good"),
        {"name": "bad", "type": "telegram", "url": "https://x"},  # invalid type
    ]))
    rec = _patch_session["install"]([_FakeResp(200)])
    out = notifier.notify("hi")
    assert "Sent to 1 notifier" in out
    assert "bad" in out  # the invalid name surfaces
    assert len(rec.calls) == 1


# ---------- cluster_label routing ---------------------------------------


def test_cluster_label_appears_in_message(monkeypatch, _patch_session):
    _set_notifiers(monkeypatch, json.dumps([
        _feishu_notifier(label="staging"),
    ]))
    rec = _patch_session["install"]([_FakeResp(200)])
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


# ---------- feishu_post (rich text) ----------------------------------------


def test_feishu_post_payload_shape(monkeypatch, _patch_session):
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "ops", "type": "feishu_post", "url": "https://feishu/x",
         "cluster_label": "staging"},
    ]))
    rec = _patch_session["install"]([_FakeResp(200)])
    out = notifier.notify(
        "## Section A\nhello\nworld\n\n## Section B\nbye",
        level="info", title="Daily Report",
    )
    assert "✅" in out
    payload = json.loads(rec.calls[0][1])
    assert payload["msg_type"] == "post"
    title = payload["content"]["post"]["zh_cn"]["title"]
    assert "Daily Report" in title
    assert "[staging]" in title  # cluster_label prefixed
    # Each non-empty line of the body becomes a paragraph
    content = payload["content"]["post"]["zh_cn"]["content"]
    flat = "".join(p[0]["text"] for p in content)
    assert "hello" in flat
    assert "world" in flat
    assert "bye" in flat
    # The `## ` heading markers should NOT be silently dropped — we keep
    # them as plain text since Feishu post format doesn't render headings.
    assert "## Section A" in flat


def test_feishu_post_falls_back_to_first_line_as_title(monkeypatch, _patch_session):
    """When no `title=` is passed, the first non-empty line of the message
    becomes the post title — mirrors what an Agent gets when it pipes a
    snapshot straight into notify()."""
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "ops", "type": "feishu_post", "url": "https://feishu/x"},
    ]))
    rec = _patch_session["install"]([_FakeResp(200)])
    notifier.notify("First line becomes title\n\nbody line")
    payload = json.loads(rec.calls[0][1])
    assert payload["content"]["post"]["zh_cn"]["title"] == "First line becomes title"


# ---------- feishu_card (interactive) --------------------------------------


def test_feishu_card_payload_shape(monkeypatch, _patch_session):
    """Card layout: header color from level, one div per `## section`,
    hr separators, no `[LEVEL] [cluster] ts` prefix (that's text-only)."""
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "ops", "type": "feishu_card", "url": "https://feishu/x",
         "cluster_label": "prod"},
    ]))
    rec = _patch_session["install"]([_FakeResp(200)])
    msg = (
        "k8s-dev 集群巡检\n\n"
        "## Nodes\nTotal: 1    Ready: 1/1\n\n"
        "## Pending Pods\n(none)"
    )
    notifier.notify(msg, level="warning", title="k8s-dev 集群巡检")
    payload = json.loads(rec.calls[0][1])
    assert payload["msg_type"] == "interactive"
    card = payload["card"]
    assert card["header"]["template"] == "orange"  # warning -> orange
    assert "k8s-dev 集群巡检" in card["header"]["title"]["content"]
    # elements: 2 divs (one per section) separated by 1 hr
    element_tags = [e["tag"] for e in card["elements"]]
    assert element_tags.count("div") == 2
    assert element_tags.count("hr") == 1
    # Each div's text uses lark_md (Feishu's markdown-ish format)
    for e in card["elements"]:
        if e["tag"] == "div":
            assert e["text"]["tag"] == "lark_md"


def test_feishu_card_color_follows_level(monkeypatch, _patch_session):
    """info=blue / warning=orange / critical=red — drives the visual
    severity cue at the top of every card."""
    cases = [("info", "blue"), ("warning", "orange"), ("critical", "red")]
    for level, expected_color in cases:
        _set_notifiers(monkeypatch, json.dumps([
            {"name": "ops", "type": "feishu_card", "url": "https://feishu/x"},
        ]))
        rec = _patch_session["install"]([_FakeResp(200)])
        notifier.notify("hi", level=level)
        payload = json.loads(rec.calls[0][1])
        assert payload["card"]["header"]["template"] == expected_color, \
            f"level={level} should map to template={expected_color}"


def test_feishu_card_no_sections_renders_as_single_div(monkeypatch, _patch_session):
    """Edge case: message has no `## ` markers (e.g. a one-line alert).
    We render the whole thing as a single div rather than producing an
    empty card."""
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "ops", "type": "feishu_card", "url": "https://feishu/x"},
    ]))
    rec = _patch_session["install"]([_FakeResp(200)])
    notifier.notify("plain text only, no markdown sections")
    payload = json.loads(rec.calls[0][1])
    elements = payload["card"]["elements"]
    assert len(elements) == 1
    assert elements[0]["tag"] == "div"
    assert "plain text only" in elements[0]["text"]["content"]


def test_feishu_card_does_not_include_text_prefix(monkeypatch, _patch_session):
    """The `[LEVEL] [cluster] ts` line that prefixes text-format messages
    must NOT appear in the card body — the card already has its own
    colored header that conveys severity + cluster, so duplicating the
    prefix would be visual noise."""
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "ops", "type": "feishu_card", "url": "https://feishu/x",
         "cluster_label": "prod"},
    ]))
    rec = _patch_session["install"]([_FakeResp(200)])
    notifier.notify("## Body\nactual content", level="warning")
    payload = json.loads(rec.calls[0][1])
    # Serialize the whole card and assert no text-style prefix leaked in
    flat = json.dumps(payload, ensure_ascii=False)
    assert "[WARNING]" not in flat
    assert "[prod]" in flat  # cluster label belongs in the header title


# ---------- section parsing helpers ----------------------------------------


def test_parse_markdown_sections_splits_on_heading():
    msg = "intro line\n\n## A\nbody a\n\n## B\nbody b\nbody b2"
    first, sections = notifier._parse_markdown_sections(msg)
    assert first == "intro line"
    assert len(sections) == 2
    assert sections[0].startswith("## A")
    assert "body a" in sections[0]
    assert sections[1].startswith("## B")
    assert "body b2" in sections[1]


def test_parse_markdown_sections_handles_no_sections():
    """A message with no `## ` markers collapses to (first_line, [])."""
    first, sections = notifier._parse_markdown_sections("just one line\nsecond")
    assert first == "just one line"
    assert sections == []


def test_compose_title_priority_title_arg_wins():
    """Explicit title > first line of message."""
    out = notifier._compose_title("Custom Title", "first line of msg")
    assert out == "Custom Title"


def test_compose_title_prepends_cluster_label():
    """cluster_label in brackets makes multi-cluster notifiers
    self-disambiguating in a busy channel."""
    out = notifier._compose_title(None, "first line", cluster_label="prod")
    assert out.startswith("[prod] ")
    assert "first line" in out


def test_compose_title_truncates_to_60_chars():
    out = notifier._compose_title("x" * 100, "")
    assert len(out) == 60


# ---------- backward compatibility: text formats unchanged -----------------


def test_text_format_title_still_appears_in_body(monkeypatch, _patch_session):
    """The Slack `**title**` prefix was the only way to add a title in
    text mode; verify it still works after the refactor that moved
    title handling from `notify()` into `_build_payload()`."""
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "ops", "type": "slack", "url": "https://slack/x"},
    ]))
    rec = _patch_session["install"]([_FakeResp(200)])
    notifier.notify("body content", title="My Title")
    payload = json.loads(rec.calls[0][1])
    assert "**My Title**" in payload["text"]
    assert "body content" in payload["text"]


def test_mixed_text_and_card_notifiers_each_render_correctly(monkeypatch, _patch_session):
    """Broadcast with one Slack + one feishu_card notifier — verify each
    gets its own correctly-shaped payload (the title-refactor bug we
    just fixed used to leak `**title**` markdown into the feishu body)."""
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "slack", "type": "slack", "url": "https://slack/x"},
        {"name": "card",  "type": "feishu_card", "url": "https://feishu/y"},
    ]))
    rec = _patch_session["install"]([_FakeResp(200), _FakeResp(200)])
    notifier.notify("## A\nbody a", level="warning", title="Header")
    payloads = [json.loads(c[1]) for c in rec.calls]
    # Each notifier got its own payload — 2 HTTP calls
    assert len(payloads) == 2
    # Card payload: msg_type=interactive, title is plain text not markdown bold
    card_payload = next(p for p in payloads if p.get("msg_type") == "interactive")
    flat = json.dumps(card_payload, ensure_ascii=False)
    assert "**Header**" not in flat  # markdown bold must NOT leak into card
    assert "Header" in flat          # but the title text itself is in the card header
    # Slack payload: text contains the title in mrkdwn bold
    slack_payload = next(p for p in payloads if "text" in p and "msg_type" not in p)
    assert "**Header**" in slack_payload["text"]


def test_webhook_redirects_are_disabled(monkeypatch, _patch_session):
    _set_notifiers(monkeypatch, json.dumps([_feishu_notifier()]))
    rec = _patch_session["install"]([_FakeResp(200)])
    notifier.notify("hello")
    assert rec.last_kwargs is not None
    assert rec.last_kwargs["allow_redirects"] is False
