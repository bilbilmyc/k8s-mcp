"""Safety primitives: HMAC-signed confirmation tokens for destructive ops.

The delete tool requires a two-step flow:
  1. confirm=False returns a preview + confirmation_token.
  2. confirm=True with the token verifies and executes the deletion.

Tokens are short-lived (settings.delete_token_ttl_seconds, default 300s) and
HMAC-signed so the server can validate without external state.

中文说明：
删除工具走二次确认流程：第一步不带 confirm 仅返回资源预览和一个
HMAC 签名 token（默认 5 分钟过期）；用户确认后第二步带 confirm=True
与 token 才真正删除。本模块负责 token 的签发、校验、过期判断，
以及确认"token 里记录的 kind/name/ns/grace_period 必须与本次删除
请求完全一致"，防止 token 被复用去删别的对象。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class TokenError(Exception):
    """Raised when a confirmation token is missing, malformed, expired, or forged."""


def issue_token(payload: dict[str, Any], secret: str, ttl_seconds: int) -> str:
    """Create a signed token. Returns the token string."""
    if not secret:
        raise TokenError("delete_token_secret is not configured")
    payload = dict(payload)
    payload["exp"] = int(time.time()) + int(ttl_seconds)
    body_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    body_b64 = base64.urlsafe_b64encode(body_bytes).decode("ascii").rstrip("=")
    sig = hmac.new(secret.encode("utf-8"), body_b64.encode("ascii"), hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")
    return f"{body_b64}.{sig_b64}"


def verify_token(token: str, secret: str) -> dict[str, Any]:
    """Validate the token's signature and expiry. Returns the payload."""
    if not token:
        raise TokenError("Missing confirmation_token")
    if not secret:
        raise TokenError("delete_token_secret is not configured")
    parts = token.split(".")
    if len(parts) != 2:
        raise TokenError("Malformed confirmation_token")
    body_b64, sig_b64 = parts
    expected_sig = base64.urlsafe_b64encode(
        hmac.new(secret.encode("utf-8"), body_b64.encode("ascii"), hashlib.sha256).digest()
    ).decode("ascii").rstrip("=")
    if not hmac.compare_digest(sig_b64, expected_sig):
        raise TokenError("Invalid confirmation_token signature")
    try:
        body_bytes = base64.urlsafe_b64decode(body_b64.encode("ascii") + b"==")
        payload = json.loads(body_bytes)
    except (ValueError, json.JSONDecodeError) as e:
        raise TokenError(f"Malformed token body: {e}") from e
    exp = int(payload.get("exp", 0))
    if exp < int(time.time()):
        raise TokenError("confirmation_token expired; please request a new preview")
    return payload


def make_delete_payload(kind: str, name: str, namespace: str | None,
                        grace_period_seconds: int,
                        caller: dict | None = None) -> dict[str, Any]:
    """Payload to sign when issuing a delete confirmation token.

    `caller` binds the token to the MCP server's authenticated kube
    identity (`{"username", "uid", "groups"}`). A leaked token cannot
    be replayed by a different MCP process running as a different user
    — `assert_payload_matches` rejects caller mismatches.
    """
    payload: dict[str, Any] = {
        "op": "delete",
        "kind": kind,
        "name": name,
        "namespace": namespace or "",
        "grace_period_seconds": int(grace_period_seconds),
    }
    if caller is not None:
        payload["caller"] = {
            "username": caller.get("username", "(unknown)"),
            "uid": caller.get("uid", ""),
        }
    return payload


def assert_payload_matches(payload: dict[str, Any], *, kind: str, name: str,
                            namespace: str | None, grace_period_seconds: int,
                            caller: dict | None = None) -> None:
    """Ensure the token's payload matches the current delete request."""
    if payload.get("op") != "delete":
        raise TokenError("Token was not issued for a delete operation")
    if payload.get("kind") != kind:
        raise TokenError(f"Token kind mismatch: {payload.get('kind')} vs {kind}")
    if payload.get("name") != name:
        raise TokenError(f"Token name mismatch: {payload.get('name')} vs {name}")
    if (payload.get("namespace") or "") != (namespace or ""):
        raise TokenError(
            f"Token namespace mismatch: {payload.get('namespace')!r} vs {namespace!r}"
        )
    if int(payload.get("grace_period_seconds", 0)) != int(grace_period_seconds):
        raise TokenError(
            f"Token grace_period mismatch: "
            f"{payload.get('grace_period_seconds')} vs {grace_period_seconds}"
        )
    if caller is not None:
        assert_caller_matches(payload.get("caller"), caller)


def assert_caller_matches(
    token_caller: dict | None, current_caller: dict,
) -> None:
    """Reject tokens issued for a different MCP-server kube identity.

    The bulk tools (`bulk_set_image`, `bulk_restart`, `bulk_scale`,
    `bulk_delete_pvc`) all sign their tokens with the issuer's
    `get_caller_identity()` snapshot. A leaked token replayed against
    a different MCP server (which is running as a different ServiceAccount
    / user) must be rejected — otherwise the "two-step confirmation"
    pattern collapses into "anyone with the token can execute".

    `token_caller` is the embedded `{"username", "uid"}` dict from the
    token's payload; `current_caller` is the live `get_caller_identity()`
    result. Mismatch on either field raises TokenError.
    """
    tc = token_caller or {}
    if tc.get("username", "") != current_caller.get("username", ""):
        raise TokenError(
            f"Token caller mismatch: issued for "
            f"{tc.get('username')!r}, current server runs as "
            f"{current_caller.get('username')!r}. A leaked token cannot be "
            "replayed across MCP servers with different identities."
        )
    # UID check is a defense-in-depth: username is the primary identity
    # claim in Kubernetes, UID is stable across renames.
    if tc.get("uid", "") != current_caller.get("uid", ""):
        raise TokenError(
            "Token caller UID mismatch — same username but different "
            "underlying identity (token replay across distinct SAs?)"
        )
