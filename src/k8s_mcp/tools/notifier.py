"""Webhook notifier: send a message to one or more configured destinations.

`notify(message, level="info", notifier_name=None)` is a thin tool that
lets the agent push a one-shot message to a Slack / Feishu / WeCom /
generic webhook. Typical use: after `cluster_health_snapshot()` returns
a report, the agent chains `notify(message=report, level="warning")` to
push the result to the team's channel — closing the "agent ran but
nobody saw it" loop on local AI-ops setups.

Notifiers are configured via env (no secret store needed):

    K8S_MCP_NOTIFIERS='[
      {"name": "ops-feishu",  "type": "feishu",
       "url": "https://open.feishu.cn/open-apis/bot/v2/hook/...",
       "cluster_label": "prod"},
      {"name": "ops-slack",   "type": "slack",
       "url": "https://hooks.slack.com/services/...",
       "cluster_label": "prod"}
    ]'

Each entry: name (id used in `notifier_name=`), type (feishu/slack/
wecom/generic), url, optional cluster_label (prefixed in the message
header so the same webhook can multiplex clusters).

Message format per type:
  - feishu:  {"msg_type":"text","content":{"text":...}}
  - wecom:   {"msgtype":"text","text":{"content":...,"mentioned_list":[]}}
  - slack:   {"text":...}
  - generic: POST raw JSON {"text":..., "level":..., "cluster_label":...}

Failure handling: any non-2xx response is captured and reported back to
the agent (so a dead webhook doesn't silently lose the message but also
doesn't bring down the calling tool). Timeouts default to 10s.

中文说明：
AI 运维场景里 MCP 跑在后台巡检、但人不一定盯着 Cherry Studio ——
本工具把巡检结果主动推送到 IM。webhook 列表走 env 配置，工具描述
里写明支持的 type（飞书 / Slack / 企微 / generic）以及每个 type 的
JSON 格式差异，Agent 选 type 即可，payload 拼装工具内部处理。
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
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
    if "type" not in n or n["type"] not in {"feishu", "slack", "wecom", "generic"}:
        return f"notifier[{idx}] ({n.get('name','?')}): 'type' must be one of feishu|slack|wecom|generic"
    if "url" not in n or not isinstance(n["url"], str) or not n["url"]:
        return f"notifier[{idx}] ({n.get('name','?')}): missing or empty 'url'"
    return None


# ---------- payload building ----------------------------------------------


def _build_payload(notifier: dict, message: str, level: str) -> dict:
    """Assemble the JSON body for the notifier's webhook.

    The header line `[LEVEL] [cluster] — ts` is prepended so the IM
    message is self-describing without the agent having to format it.
    """
    cluster_label = notifier.get("cluster_label", "").strip()
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    prefix_bits = [_LEVEL_PREFIX.get(level, "[INFO]")]
    if cluster_label:
        prefix_bits.append(f"[{cluster_label}]")
    prefix_bits.append(ts)
    header = " ".join(prefix_bits)
    body = f"{header}\n{message}"

    typ = notifier["type"]
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
          NOTIFIER  TYPE      STATUS  DETAIL
          ops       feishu    ✅      200 OK — body: {"StatusCode":0,...}
          oncall    slack     ❌      HTTP 401 — body: invalid_token

        On configuration errors (no notifiers, bad name, bad JSON),
        returns a clear Chinese error message naming the fix path
        (`K8S_MCP_NOTIFIERS=...`).
    """
    notifiers = _parse_notifiers()
    if not notifiers:
        return (
            "❌ No notifiers configured. Set `K8S_MCP_NOTIFIERS` to a JSON "
            "list of `{\"name\":..., \"type\": feishu|slack|wecom|generic, "
            "\"url\":...}` and restart the MCP server. Example:\n"
            "  K8S_MCP_NOTIFIERS='[{\"name\":\"ops\",\"type\":\"feishu\","
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

    # Prepend the title if given
    if title:
        message = f"**{title}**\n{message}" if any(
            n["type"] == "slack" for n in valid
        ) else f"{title}\n{message}"

    # Send
    rows: list[dict] = []
    for n in valid:
        payload = _build_payload(n, message, level)
        ok, detail = _post(n["url"], payload)
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
