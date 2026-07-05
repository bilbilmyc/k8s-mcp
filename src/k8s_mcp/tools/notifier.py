"""Webhook notifier: send a message to one or more configured destinations.

`notify(message, level="info", notifier_name=None)` is a thin tool that
lets the agent push a one-shot message to a Slack / Feishu / WeCom /
generic webhook. Typical use: after `cluster_health_snapshot()` returns
a report, the agent chains `notify(message=report, level="warning")` to
push the result to the team's channel — closing the "agent ran but
nobody saw it" loop on local AI-ops setups.

Notifiers are configured via env (no secret store needed):

    K8S_MCP_NOTIFIERS='[
      {"name": "ops-feishu",  "type": "feishu_card",
       "url": "https://open.feishu.cn/open-apis/bot/v2/hook/...",
       "cluster_label": "prod"},
      {"name": "ops-slack",   "type": "slack",
       "url": "https://hooks.slack.com/services/...",
       "cluster_label": "prod"}
    ]'

Each entry: name (id used in `notifier_name=`), type (one of feishu /
feishu_post / feishu_card / slack / wecom / generic), url, optional
cluster_label (prefixed in the message header so the same webhook can
multiplex clusters).

Message format per type:
  - feishu:        {"msg_type":"text","content":{"text":...}}  (plain)
  - feishu_post:   {"msg_type":"post","content":{"post":{...}}}  (rich text)
  - feishu_card:   {"msg_type":"interactive","card":{...}}       (card with color header)
  - wecom:         {"msgtype":"text","text":{"content":...,"mentioned_list":[]}}
  - slack:         {"text":...}
  - generic:       POST raw JSON {"text":..., "level":..., "cluster_label":...}

`feishu_post` is the middle ground: rich text with title + paragraphs,
no buttons, no color header. `feishu_card` is the full interactive
card — header color follows `level` (info=blue, warning=orange,
critical=red), each `## section` block in the message becomes a
`div` element with `lark_md` content, sections separated by `hr`.

Both rich variants parse the message into sections using the
`## Heading` convention that `cluster_health_snapshot` already
produces — so an Agent that pipes a snapshot straight into
`notify(message=snapshot_text)` gets a readable card without
having to format anything itself.

Failure handling: any non-2xx response is captured and reported back to
the agent (so a dead webhook doesn't silently lose the message but also
doesn't bring down the calling tool). Timeouts default to 10s.

中文说明：
AI 运维场景里 MCP 跑在后台巡检、但人不一定盯着 Cherry Studio ——
本工具把巡检结果主动推送到 IM。webhook 列表走 env 配置，工具描述
里写明支持的 type（飞书文本 / 飞书富文本 post / 飞书交互卡片 card /
Slack / 企微 / generic）以及每个 type 的 JSON 格式差异，Agent 选
type 即可，payload 拼装工具内部处理。`feishu_card` 是推荐的
生产用法：header 颜色随 level 变化，每个 `## 章节` 渲染成独立块，
飞书原生支持排版，operator 在手机上扫一眼就能定位异常段落。
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from ..config import get_settings

logger = logging.getLogger(__name__)


_LEVEL_PREFIX = {
    "info": "ℹ️  [INFO]",
    "warning": "⚠️  [WARNING]",
    "critical": "❌ [CRITICAL]",
}


# ---------- notifier registry ---------------------------------------------


def _parse_notifiers() -> list[dict]:
    """Parse K8S_MCP_NOTIFIERS env into a list of dicts.

    Malformed JSON / wrong shape → empty list + a warning. We never raise
    from here: a misconfigured notifier list should not take down the
    `notify` tool entirely.
    """
    raw = get_settings().notifiers
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("K8S_MCP_NOTIFIERS is not valid JSON: %s", e)
        return []
    if not isinstance(parsed, list):
        logger.warning("K8S_MCP_NOTIFIERS must be a JSON list, got %s", type(parsed).__name__)
        return []
    return [n for n in parsed if isinstance(n, dict)]


def _validate_notifier(n: dict, idx: int) -> str | None:
    """Return an error message if `n` is missing required fields, else None."""
    if "name" not in n or not isinstance(n["name"], str) or not n["name"]:
        return f"notifier[{idx}]: missing or empty 'name'"
    if "type" not in n or n["type"] not in {"feishu", "feishu_post", "feishu_card",
                                            "slack", "wecom", "generic"}:
        return f"notifier[{idx}] ({n.get('name','?')}): 'type' must be one of feishu|feishu_post|feishu_card|slack|wecom|generic"
    if "url" not in n or not isinstance(n["url"], str) or not n["url"]:
        return f"notifier[{idx}] ({n.get('name','?')}): missing or empty 'url'"
    return None


# ---------- payload building ----------------------------------------------


_LEVEL_HEADER_COLOR = {
    "info": "blue",
    "warning": "orange",
    "critical": "red",
}


def _parse_markdown_sections(message: str) -> tuple[str, list[str]]:
    """Split a `## section\\n...` message into (pre_heading_text, [section_blocks]).

    `pre_heading_text` is any text that appears BEFORE the first `## `
    heading (used as a fallback title when the caller didn't pass one
    explicitly). Empty when the message starts with a heading.

    `section_blocks` is the list of `## Name\\n...` blocks. Returns
    `([], [])` when the message has no `## ` markers at all — the caller
    then renders the whole message as a single block (fallback path).

    Designed for the output shape of `cluster_health_snapshot` (markdown
    sections delimited by `## `).
    """
    lines = message.splitlines()

    # Skip leading blank lines to find the first content line.
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines):
        return "", []

    # Locate the first `## ` heading (anywhere from the first content line).
    first_heading_idx: int | None = None
    for j in range(i, len(lines)):
        if lines[j].startswith("## "):
            first_heading_idx = j
            break

    if first_heading_idx is None:
        # No headings anywhere — the whole message is the title/fallback.
        return lines[i].strip(), []

    # Anything between the first content line and the first heading is
    # pre-section text. If the first content line IS the first heading,
    # pre-section text is empty (the heading itself is the start of
    # section_blocks below).
    pre = "\n".join(lines[i:first_heading_idx]).strip() if first_heading_idx > i else ""

    sections: list[str] = []
    current: list[str] = []
    for ln in lines[first_heading_idx:]:
        if ln.startswith("## "):
            if current:
                sections.append("\n".join(current).strip())
                current = []
            current.append(ln)
        else:
            current.append(ln)
    if current:
        sections.append("\n".join(current).strip())

    return pre, [s for s in sections if s]


def _compose_title(
    title: str | None, message: str, cluster_label: str = "",
) -> str:
    """Pick the title to put in card / post header.

    Priority: explicit `title` arg > first non-empty line of `message`.
    `cluster_label` is prepended in brackets so multi-cluster notifiers
    stay disambiguated; truncated to 60 chars (Feishu card header limit).
    """
    chosen = (title or "").strip()
    if not chosen:
        first, _ = _parse_markdown_sections(message)
        chosen = first
    chosen = chosen.strip()
    if cluster_label:
        chosen = f"[{cluster_label}] {chosen}" if chosen else f"[{cluster_label}]"
    return chosen[:60] or "Notification"


def _build_feishu_card(
    message: str, title: str | None, level: str, cluster_label: str = "",
) -> dict:
    """Build a Feishu interactive card payload from a structured message.

    Each `## section` block becomes a card `div` with `lark_md` content;
    sections separated by `hr`. Header color is derived from `level`.
    If the message has no `## ` markers, the whole body is rendered as
    one div (the "we got something weird but it's still rendered" path).
    """
    card_title = _compose_title(title, message, cluster_label)
    _, sections = _parse_markdown_sections(message)

    elements: list[dict] = []
    if not sections:
        body = message.strip() or "(empty)"
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": body[:2000]},
        })
    else:
        for i, s in enumerate(sections):
            if i > 0:
                elements.append({"tag": "hr"})
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": s[:2000]},
            })

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": card_title},
                "template": _LEVEL_HEADER_COLOR.get(level, "blue"),
            },
            "elements": elements,
        },
    }


def _build_feishu_post(
    message: str, title: str | None, cluster_label: str = "",
) -> dict:
    """Build a Feishu post (rich text) payload — middle ground between
    plain text and full cards. First non-empty line is the title;
    remaining lines become content paragraphs. `lark_md` is not
    supported in post format, so we just emit plain text lines (the
    `## ` heading markers are kept — they're harmless in plain text).
    """
    post_title = _compose_title(title, message, cluster_label)

    content_lines: list[list[dict]] = []
    # If the message has `## ` sections, emit one paragraph per line so
    # blank lines visually separate sections in Feishu. Otherwise emit
    # the whole message line by line.
    _, sections = _parse_markdown_sections(message)
    source = sections if sections else [message]
    for blk in source:
        for ln in blk.splitlines():
            stripped = ln.strip()
            if stripped:
                content_lines.append([{"tag": "text", "text": stripped[:200]}])
    if not content_lines:
        content_lines.append([{"tag": "text", "text": "(empty)"}])

    return {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": post_title,
                    "content": content_lines[:50],  # Feishu post limit
                }
            }
        },
    }


def _build_payload(
    notifier: dict, message: str, level: str, title: str | None = None,
) -> dict:
    """Assemble the JSON body for the notifier's webhook.

    For rich Feishu variants (`feishu_post` / `feishu_card`) the message
    is parsed into sections; the title is used in the card/post header
    instead of being prepended to a body blob.

    For text-style formats (`feishu` / `wecom` / `slack` / `generic`) the
    header line `[LEVEL] [cluster] — ts` is prepended so the IM message
    is self-describing without the agent having to format it.
    """
    typ = notifier["type"]
    cluster_label = notifier.get("cluster_label", "").strip()

    if typ == "feishu_card":
        return _build_feishu_card(message, title, level, cluster_label)
    if typ == "feishu_post":
        return _build_feishu_post(message, title, cluster_label)

    # Text-style formats.
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    prefix_bits = [_LEVEL_PREFIX.get(level, "[INFO]")]
    if cluster_label:
        prefix_bits.append(f"[{cluster_label}]")
    prefix_bits.append(ts)
    header = " ".join(prefix_bits)
    body = f"{header}\n{message}"
    if title:
        body = f"**{title}**\n{body}" if typ == "slack" else f"{title}\n{body}"

    if typ == "feishu":
        return {"msg_type": "text", "content": {"text": body}}
    if typ == "wecom":
        return {"msgtype": "text", "text": {"content": body, "mentioned_list": []}}
    if typ == "slack":
        # Use mrkdwn formatting for emoji prefixes
        return {"text": body}
    # generic: include the structured fields so receivers can branch on them
    return {
        "text": body,
        "level": level,
        "cluster_label": cluster_label,
        "timestamp": ts,
    }


# ---------- send -----------------------------------------------------------


def _post(url: str, payload: dict, timeout: int = 10) -> tuple[bool, str]:
    """POST JSON. Returns (ok, detail). detail is HTTP status + reason on
    failure, or the HTTP status line on success."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "k8s-mcp/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = f"{resp.status} {resp.reason}"
            # Some IM APIs (Feishu) return {"StatusCode": 0, "Msg": "success"} in body.
            try:
                body = resp.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                body = ""
            if 200 <= resp.status < 300:
                return True, status + (f" — body: {body[:200]}" if body else "")
            return False, status + (f" — body: {body[:200]}" if body else "")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        return False, f"HTTP {e.code} {e.reason}" + (f" — body: {body}" if body else "")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return False, f"{type(e).__name__}: {e}"


# ---------- entry point ----------------------------------------------------


def notify(
    message: str,
    level: str = "info",
    notifier_name: str | None = None,
    title: str | None = None,
) -> str:
    """📤 NOTIFY — POST a message to one or all configured webhook(s).

    Use this to push the result of a read-only tool (typically
    `cluster_health_snapshot`) to a team's IM channel so the operator
    sees the report even when they aren't sitting in front of Cherry
    Studio. Pairs naturally with periodic / on-demand snapshot calls.

    Args:
        message: the body. Plain text; notifier-type-specific formatting
            (markdown / @mention) is NOT applied — the agent is
            responsible for writing content the IM can render. Long
            messages are sent as-is; most IM bots truncate around 4-6 KB.
        level: `info` (default, ℹ️), `warning` (⚠️), or `critical` (❌).
            The level is rendered as a prefix so receivers can route on it
            (e.g. critical triggers a phone-push in some IM clients).
        notifier_name: when set, send only to the notifier with this
            `name` from the env config. When None (default), broadcast
            to every configured notifier. Use this to scope ("just the
            on-call channel, not the #all-ops channel").
        title: optional first line rendered above the message body, in
            bold for Slack and plain for the rest. Useful for
            "Cluster X health report" style headers.

    Returns:
        A per-notifier results table:
          NOTIFIER  TYPE         STATUS  DETAIL
          ops       feishu_card  ✅      200 OK — body: {"StatusCode":0,...}
          oncall    slack        ❌      HTTP 401 — body: invalid_token

    Output type per notifier config (set at deploy time, not per call):
      - `feishu`         plain text (`msg_type=text`)
      - `feishu_post`    Feishu rich text (`msg_type=post`) — title +
                         paragraph lines, no color header, no buttons
      - `feishu_card`    Feishu interactive card (`msg_type=interactive`) —
                         color header tied to `level`, each `## section`
                         block in the message becomes a `div` with `lark_md`
                         content, sections separated by `hr`. **Recommended**
                         for health-snapshot pushes.
      - `wecom` / `slack` / `generic`  text only.

        On configuration errors (no notifiers, bad name, bad JSON),
        returns a clear Chinese error message naming the fix path
        (`K8S_MCP_NOTIFIERS=...`).
    """
    notifiers = _parse_notifiers()
    if not notifiers:
        return (
            "❌ No notifiers configured. Set `K8S_MCP_NOTIFIERS` to a JSON "
            "list of `{\"name\":..., \"type\": feishu|feishu_post|feishu_card|slack|wecom|generic, "
            "\"url\":...}` and restart the MCP server. Example:\n"
            "  K8S_MCP_NOTIFIERS='[{\"name\":\"ops\",\"type\":\"feishu_card\","
            "\"url\":\"https://open.feishu.cn/open-apis/bot/v2/hook/...\"}]'"
        )

    # Validate each notifier
    valid: list[dict] = []
    validation_errors: list[str] = []
    for i, n in enumerate(notifiers):
        err = _validate_notifier(n, i)
        if err:
            validation_errors.append(err)
        else:
            valid.append(n)

    # Filter by name if specified
    if notifier_name is not None:
        valid = [n for n in valid if n["name"] == notifier_name]
        if not valid:
            available = sorted({n["name"] for n in notifiers if "name" in n})
            return (
                f"❌ Notifier {notifier_name!r} not found. "
                f"Available: {available or '(none)'}."
            )

    if level not in _LEVEL_PREFIX:
        return (
            f"❌ Invalid level {level!r}. Must be one of: "
            f"{sorted(_LEVEL_PREFIX)}."
        )

    if validation_errors and not valid:
        # All notifiers are malformed — refuse to do anything
        return "❌ All configured notifiers are invalid:\n  " + \
            "\n  ".join(validation_errors)

    # Title handling: for rich Feishu variants the title lands in the
    # card/post header; for text formats it goes at the top of the body
    # (Slack mrkdwn, plain for the rest). `_build_payload` decides per
    # notifier — keep `message` and `title` separate so we don't pollute
    # one channel's body with another channel's prefix.
    #
    # Concurrent send: each notifier is independent (different webhook URL),
    # so we fan out across threads rather than paying the latency of N
    # serial HTTP requests. With 1-2 notifiers this is negligible; with
    # 5+ (common on AI-ops setups that broadcast to ops + oncall + audit)
    # it cuts wall-clock time proportionally. Timeout per notifier is
    # bounded by `_post(timeout=10)`, so even if one webhook hangs the
    # whole broadcast completes within a few seconds.
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(len(valid), 1)) as pool:
        futures = {
            pool.submit(_post, n["url"], _build_payload(n, message, level, title)): n
            for n in valid
        }
        for fut, n in futures.items():
            ok, detail = fut.result()
            rows.append({
                "NOTIFIER": n["name"],
                "TYPE": n["type"],
                "STATUS": "✅" if ok else "❌",
                "DETAIL": detail,
            })

    if validation_errors:
        # Some succeeded, some were malformed — surface both
        skipped = "\n  ".join(validation_errors)
        return f"⚠️ Sent to {len(valid)} notifier(s), skipped invalid configs:\n  {skipped}\n\n" + \
            _render_table(rows)

    return _render_table(rows)


def _render_table(rows: list[dict]) -> str:
    from ..formatters import short_table
    if not rows:
        return "(no notifiers to send to)"
    return short_table(rows, ["NOTIFIER", "TYPE", "STATUS", "DETAIL"])


def register(mcp) -> None:
    mcp.tool()(notify)
