"""Safety primitives: HMAC-signed confirmation tokens for destructive ops.

The delete tool requires a two-step flow:
  1. confirm=False returns a preview + confirmation_token.
  2. confirm=True with the token verifies and executes the deletion.

Tokens are short-lived (settings.delete_token_ttl_seconds, default 300s) and
HMAC-signed so the server can validate without external state.
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
                        grace_period_seconds: int) -> dict[str, Any]:
    """Payload to sign when issuing a delete confirmation token."""
    return {
        "op": "delete",
        "kind": kind,
        "name": name,
        "namespace": namespace or "",
        "grace_period_seconds": int(grace_period_seconds),
    }


def assert_payload_matches(payload: dict[str, Any], *, kind: str, name: str,
                            namespace: str | None, grace_period_seconds: int) -> None:
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
