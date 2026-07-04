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


# ---------- feishu_post (rich text) ----------------------------------------


def test_feishu_post_payload_shape(monkeypatch, _patch_urlopen):
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "ops", "type": "feishu_post", "url": "https://feishu/x",
         "cluster_label": "staging"},
    ]))
    rec = _patch_urlopen["install"](_FakeResp(200))
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


def test_feishu_post_falls_back_to_first_line_as_title(monkeypatch, _patch_urlopen):
    """When no `title=` is passed, the first non-empty line of the message
    becomes the post title — mirrors what an Agent gets when it pipes a
    snapshot straight into notify()."""
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "ops", "type": "feishu_post", "url": "https://feishu/x"},
    ]))
    rec = _patch_urlopen["install"](_FakeResp(200))
    notifier.notify("First line becomes title\n\nbody line")
    payload = json.loads(rec.calls[0][1])
    assert payload["content"]["post"]["zh_cn"]["title"] == "First line becomes title"


# ---------- feishu_card (interactive) --------------------------------------


def test_feishu_card_payload_shape(monkeypatch, _patch_urlopen):
    """Card layout: header color from level, one div per `## section`,
    hr separators, no `[LEVEL] [cluster] ts` prefix (that's text-only)."""
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "ops", "type": "feishu_card", "url": "https://feishu/x",
         "cluster_label": "prod"},
    ]))
    rec = _patch_urlopen["install"](_FakeResp(200))
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


def test_feishu_card_color_follows_level(monkeypatch, _patch_urlopen):
    """info=blue / warning=orange / critical=red — drives the visual
    severity cue at the top of every card."""
    cases = [("info", "blue"), ("warning", "orange"), ("critical", "red")]
    for level, expected_color in cases:
        _set_notifiers(monkeypatch, json.dumps([
            {"name": "ops", "type": "feishu_card", "url": "https://feishu/x"},
        ]))
        rec = _patch_urlopen["install"](_FakeResp(200))
        notifier.notify("hi", level=level)
        payload = json.loads(rec.calls[0][1])
        assert payload["card"]["header"]["template"] == expected_color, \
            f"level={level} should map to template={expected_color}"


def test_feishu_card_no_sections_renders_as_single_div(monkeypatch, _patch_urlopen):
    """Edge case: message has no `## ` markers (e.g. a one-line alert).
    We render the whole thing as a single div rather than producing an
    empty card."""
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "ops", "type": "feishu_card", "url": "https://feishu/x"},
    ]))
    rec = _patch_urlopen["install"](_FakeResp(200))
    notifier.notify("plain text only, no markdown sections")
    payload = json.loads(rec.calls[0][1])
    elements = payload["card"]["elements"]
    assert len(elements) == 1
    assert elements[0]["tag"] == "div"
    assert "plain text only" in elements[0]["text"]["content"]


def test_feishu_card_does_not_include_text_prefix(monkeypatch, _patch_urlopen):
    """The `[LEVEL] [cluster] ts` line that prefixes text-format messages
    must NOT appear in the card body — the card already has its own
    colored header that conveys severity + cluster, so duplicating the
    prefix would be visual noise."""
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "ops", "type": "feishu_card", "url": "https://feishu/x",
         "cluster_label": "prod"},
    ]))
    rec = _patch_urlopen["install"](_FakeResp(200))
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


def test_text_format_title_still_appears_in_body(monkeypatch, _patch_urlopen):
    """The Slack `**title**` prefix was the only way to add a title in
    text mode; verify it still works after the refactor that moved
    title handling from `notify()` into `_build_payload()`."""
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "ops", "type": "slack", "url": "https://slack/x"},
    ]))
    rec = _patch_urlopen["install"](_FakeResp(200))
    notifier.notify("body content", title="My Title")
    payload = json.loads(rec.calls[0][1])
    assert "**My Title**" in payload["text"]
    assert "body content" in payload["text"]


def test_mixed_text_and_card_notifiers_each_render_correctly(monkeypatch, _patch_urlopen):
    """Broadcast with one Slack + one feishu_card notifier — verify each
    gets its own correctly-shaped payload (the title-refactor bug we
    just fixed used to leak `**title**` markdown into the feishu body)."""
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "slack", "type": "slack", "url": "https://slack/x"},
        {"name": "card",  "type": "feishu_card", "url": "https://feishu/y"},
    ]))
    rec = _patch_urlopen["install"](_FakeResp(200))
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


# ---------- markdown table → Feishu table component -----------------------


def test_split_row_basic():
    assert notifier._split_row("| a | b |") == ["a", "b"]
    assert notifier._split_row("|  a  |  b  |") == ["a", "b"]
    assert notifier._split_row("| a || c |") == ["a", "", "c"]
    assert notifier._split_row("  | x | y |  ") == ["x", "y"]


def test_split_row_rejects_non_table_lines():
    assert notifier._split_row("just text") is None
    assert notifier._split_row("| trailing pipe") is None
    assert notifier._split_row("leading pipe |") is None
    assert notifier._split_row("") is None
    assert notifier._split_row("   ") is None


def test_parse_markdown_table_happy_path():
    lines = [
        "| # | Pod | CPU |",
        "|---|---|---|",
        "| 1 | apiserver | 5.6 |",
        "| 2 | etcd | 2.6 |",
        "| 3 | grafana | 0.9 |",
    ]
    result = notifier._parse_markdown_table(lines, 0)
    assert result is not None
    headers, body, consumed = result
    assert headers == ["#", "Pod", "CPU"]
    assert body == [["1", "apiserver", "5.6"], ["2", "etcd", "2.6"], ["3", "grafana", "0.9"]]
    assert consumed == 5


def test_parse_markdown_table_alignment_markers():
    lines = [
        "| left | center | right |",
        "|:-----|:------:|------:|",
        "| 1 | 2 | 3 |",
    ]
    result = notifier._parse_markdown_table(lines, 0)
    assert result is not None
    assert result[0] == ["left", "center", "right"]


def test_parse_markdown_table_rejects_non_tables():
    # No separator row
    assert notifier._parse_markdown_table(["| a |", "| b |"], 0) is None
    # Bad separator (text instead of dashes)
    assert notifier._parse_markdown_table(["| a | b |", "| x | y |"], 0) is None
    # Column count mismatch between header and separator
    assert notifier._parse_markdown_table(
        ["| a | b |", "|---|---|---|", "| 1 | 2 |"], 0,
    ) is None
    # Just the header line (no body rows) — not a table worth rendering
    assert notifier._parse_markdown_table(
        ["| a | b |", "|---|---|"], 0,
    ) is None
    # Plain text
    assert notifier._parse_markdown_table(["hello", "world"], 0) is None


def test_parse_markdown_table_stops_at_non_table_line():
    """A table can be followed by non-table lines; the parser should
    stop at the first non-row line and report the right `consumed`."""
    lines = [
        "| col |",
        "|---|",
        "| a |",
        "| b |",
        "",  # blank
        "after",
    ]
    result = notifier._parse_markdown_table(lines, 0)
    assert result is not None
    _, body, consumed = result
    assert body == [["a"], ["b"]]
    assert consumed == 4


def test_build_feishu_table_pads_and_truncates():
    headers = ["a", "b", "c"]
    rows = [
        ["1", "2"],          # short — should be padded
        ["x", "y", "z", "w"],  # long — should be truncated
    ]
    el = notifier._build_feishu_table(headers, rows)
    assert el["tag"] == "table"
    table_rows = el["rows"]
    # 1 header + 2 body = 3 rows
    assert len(table_rows) == 3
    # Header row cells
    assert [c["text"] for c in table_rows[0]] == ["a", "b", "c"]
    # First data row padded to 3 cols
    assert [c["text"] for c in table_rows[1]] == ["1", "2", ""]
    # Second data row truncated to 3 cols (drops "w")
    assert [c["text"] for c in table_rows[2]] == ["x", "y", "z"]


def test_section_to_elements_emits_table_for_markdown_table():
    section = (
        "## 🔥 Pod CPU Top10\n\n"
        "| # | Pod | CPU |\n"
        "|---|---|---|\n"
        "| 1 | apiserver | 5.6 |\n"
        "| 2 | etcd | 2.6 |\n"
    )
    els = notifier._section_to_elements(section)
    # Expect: one div (heading + intro empty) then one table
    assert [e["tag"] for e in els] == ["div", "table"]
    # The div carries the heading via lark_md
    assert "Pod CPU Top10" in els[0]["text"]["content"]
    # The table has the right rows
    table_rows = els[1]["rows"]
    assert [c["text"] for c in table_rows[0]] == ["#", "Pod", "CPU"]
    assert [c["text"] for c in table_rows[1]] == ["1", "apiserver", "5.6"]
    assert [c["text"] for c in table_rows[2]] == ["2", "etcd", "2.6"]


def test_section_to_elements_text_only_no_table():
    """A section with no table should still render as a single div
    (backward-compat with the pre-table implementation)."""
    section = "## Heading\nSome bullet\n- item 1\n- item 2"
    els = notifier._section_to_elements(section)
    assert len(els) == 1
    assert els[0]["tag"] == "div"
    assert "Heading" in els[0]["text"]["content"]
    assert "- item 1" in els[0]["text"]["content"]


def test_section_to_elements_text_around_table():
    """`intro text + table + closing text` becomes three elements in
    order. This is the layout a real health snapshot uses."""
    section = (
        "## Resource\n"
        "Top CPU pods:\n"
        "| Pod | CPU |\n"
        "|---|---|\n"
        "| a | 1 |\n"
        "| b | 2 |\n"
        "\n"
        "Conclusion: cluster healthy.\n"
    )
    els = notifier._section_to_elements(section)
    assert [e["tag"] for e in els] == ["div", "table", "div"]
    # First div: heading + intro ("Top CPU pods:")
    assert "Top CPU pods" in els[0]["text"]["content"]
    # Middle: the table itself
    assert els[1]["tag"] == "table"
    assert [c["text"] for c in els[1]["rows"][1]] == ["a", "1"]
    # Last div: closing text
    assert "Conclusion: cluster healthy" in els[2]["text"]["content"]


def test_feishu_card_with_table_produces_native_table_element(monkeypatch, _patch_urlopen):
    """End-to-end: sending a message with a markdown table via notify()
    produces a Feishu card whose `elements` contain a `table` element
    — not a `div` with the raw markdown text."""
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "ops", "type": "feishu_card", "url": "https://feishu/x"},
    ]))
    rec = _patch_urlopen["install"](_FakeResp(200))
    msg = (
        "## 🔥 Pod CPU Top10\n\n"
        "| # | Pod | CPU |\n"
        "|---|---|---|\n"
        "| 1 | apiserver | 5.6 |\n"
        "| 2 | etcd | 2.6 |\n"
    )
    notifier.notify(msg)
    payload = json.loads(rec.calls[0][1])
    elements = payload["card"]["elements"]
    tags = [e["tag"] for e in elements]
    assert "table" in tags, f"expected a 'table' element, got tags={tags}"
    # The table element should NOT contain the raw `|` pipes
    table_el = next(e for e in elements if e["tag"] == "table")
    flat = json.dumps(table_el, ensure_ascii=False)
    assert "|" not in flat, "raw markdown pipes should not leak into the table element"
    # And the header row is present
    assert [c["text"] for c in table_el["rows"][0]] == ["#", "Pod", "CPU"]


def test_feishu_card_multiple_tables_one_per_section(monkeypatch, _patch_urlopen):
    """Multiple `## ` sections, each with its own table — one table
    element per section, separated by `hr`."""
    _set_notifiers(monkeypatch, json.dumps([
        {"name": "ops", "type": "feishu_card", "url": "https://feishu/x"},
    ]))
    rec = _patch_urlopen["install"](_FakeResp(200))
    msg = (
        "## CPU\n"
        "| Pod | CPU |\n|---|---|\n| a | 1 |\n"
        "\n"
        "## Memory\n"
        "| Pod | Mem |\n|---|---|\n| b | 2 |\n"
    )
    notifier.notify(msg)
    payload = json.loads(rec.calls[0][1])
    elements = payload["card"]["elements"]
    table_count = sum(1 for e in elements if e["tag"] == "table")
    hr_count = sum(1 for e in elements if e["tag"] == "hr")
    assert table_count == 2
    assert hr_count == 1  # one separator between two sections
