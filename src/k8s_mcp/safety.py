"""Safety primitives: HMAC-signed confirmation tokens, rate limit, per-call
timeout, and apiserver-error sanitization for destructive ops.

The delete tool requires a two-step flow:
  1. confirm=False returns a preview + confirmation_token.
  2. confirm=True with the token verifies and executes the deletion.

Tokens are short-lived (settings.delete_token_ttl_seconds, default 300s) and
HMAC-signed so the server can validate without external state.

This module also owns three operational safety nets applied at the
FastMCP `call_tool` boundary (in server.py):

  - `TokenBucket` / `RateLimiter` — per-tool in-memory token bucket so a
    runaway agent can't fan out `list_pods` / `get_pod_logs` and saturate
    the apiserver or the MCP transport. Default 120 RPM per tool, reset
    on process restart.
  - `safe_apiserver_error` — maps raw `kubernetes.client.rest.ApiException`
    values to a curated `SafeApiError` whose message is a one-liner with
    status + operation context but **not** the apiserver's verbose body.
    The raw `body` / `reason` are not exposed to the LLM — they may
    contain RBAC details, internal hostnames, audit-trail fragments, etc.
  - `SafeApiError` itself — the exception type that the call_tool
    boundary raises. Tool code can keep using `raise RuntimeError(...)`
    or letting `ApiException` propagate; the boundary normalizes.

中文说明：
本模块除了签发 / 校验 HMAC delete token 外，还提供三个生产级安全
兜底（都在 server.py 的 call_tool 边界统一接入）：

  - 速率限制：per-tool token bucket，进程内，默认每工具 120 RPM；
    防止失控的 agent 反复 list / get 把 apiserver 刷爆或 MCP 通道撑爆。
  - apiserver 错误脱敏：把 k8s client 抛出的 `ApiException`（status /
    reason / body 含 RBAC 细节、内部 hostname、审计片段）转成只暴露
    status + 标准化一句话摘要的 `SafeApiError`，避免把 K8s 内部信息
    直接喂给 LLM。
  - 单次调用超时：包一层 `asyncio.wait_for` 防止单个 tool 卡死把整个
    MCP 会会拖死。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import threading
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


# ===========================================================================
# Apiserver error sanitization (P1-4)
# ===========================================================================
#
# The kubernetes Python client raises `ApiException` with `.status` (HTTP
# code), `.reason` (one of a small set: "Not Found", "Forbidden", ...),
# and `.body` (a JSON string with the apiserver's full error envelope —
# `kind`, `apiVersion`, `status`, `message`, `reason`, `details`, plus
# `code` and `metadata`). The `message` and `details` often leak K8s
# internals that an LLM should not be exposed to verbatim:
#
#   - 403 Forbidden bodies include the requesting user's groups, the
#     attempted verb+resource+namespace, and the missing permission's
#     name — useful info for the *operator*, not for the *LLM* that's
#     trying to decide what to do next.
#   - 422 Invalid bodies include the offending field path, which
#     exposes manifest structure.
#   - 500 Server Error bodies sometimes include stack frames and
#     internal hostnames.
#
# `safe_apiserver_error` maps the raw exception to a `SafeApiError` whose
# message is a one-line summary, suitable for the LLM, with a hint about
# the next tool to try (e.g. "use `whoami` to see effective permissions"
# for 403s) so the agent can recover without re-asking the user.

# A small allowlist of (status, reason) -> (one-liner, optional hint) pairs.
# Anything not matched falls through to a generic "{op} on {kind}/{name}
# failed with apiserver HTTP {status}" message — status only, no body.
_API_ERROR_TABLE: dict[tuple[int, str], tuple[str, str | None]] = {
    (401, ""): (
        "unauthenticated request to apiserver",
        "check KUBECONFIG / K8S_MCP_API_TOKEN validity",
    ),
    (403, ""): (
        "RBAC denied",
        "use `whoami` to see effective permissions for the current identity",
    ),
    (404, ""): (
        "resource not found",
        None,
    ),
    (409, ""): (
        "resource version conflict (concurrent modification)",
        "re-read the resource and retry",
    ),
    (422, ""): (
        "manifest validation error",
        "diff against the live resource with `diff_resource`",
    ),
    (429, ""): (
        "apiserver rate-limited this client",
        "back off and retry after a few seconds",
    ),
    (500, ""): (
        "apiserver internal error",
        "this is an operator-side issue, not the manifest",
    ),
    (503, ""): (
        "apiserver temporarily unavailable",
        "retry with backoff",
    ),
    (504, ""): (
        "apiserver timeout",
        "the cluster may be under load; retry with backoff",
    ),
}


class SafeApiError(Exception):
    """Apiserver error sanitized for LLM consumption.

    Carries the same `status` / `reason` as the underlying ApiException
    (so `call_tool` can log them at the operator level) but exposes only
    a curated `message` to the LLM — the apiserver's verbose body is
    dropped on the floor.

    `hint` is a short suggestion for the *next* tool to try (e.g. "use
    `whoami` to see effective permissions" for 403s). The promotion of
    next-step guidance directly in the error message — instead of only
    in the discovery tool's docstring — is intentional: agents re-read
    errors, not sibling docstrings.
    """

    def __init__(self, status: int, reason: str, message: str, hint: str | None = None):
        self.status = int(status)
        self.reason = reason or ""
        self.hint = hint
        super().__init__(message)


def safe_apiserver_error(
    e: Exception,
    *,
    op: str,
    kind: str,
    name: str = "",
    namespace: str = "",
) -> SafeApiError:
    """Map a raw exception (typically `ApiException`) to a `SafeApiError`.

    `op` is the user-facing operation label ("read", "create", "delete",
    "list", "patch", ...). It's included in the message so the LLM
    knows which call failed. `kind` / `name` / `namespace` are
    appended as a short resource identifier — never the full body.

    For non-`ApiException` exceptions (e.g. `ValueError` from a
    pre-flight check, `ConnectionError` from network), we surface a
    generic message with the exception's class name but NOT its
    `args`, since those may include credentials or other secrets
    (e.g. `urllib3` sometimes embeds the URL+headers in a connection
    error message).
    """
    from kubernetes.client.rest import ApiException  # local: avoid hard dep at import

    if isinstance(e, ApiException):
        status = int(getattr(e, "status", 0) or 0)
        reason = getattr(e, "reason", "") or ""
        one_liner, hint = _API_ERROR_TABLE.get(
            (status, ""), _API_ERROR_TABLE.get((status, reason), (
                f"apiserver returned HTTP {status}",
                None,
            ))
        )
        # Resource identifier — keep terse.
        who = f"{kind}/{name}" if name else kind
        if namespace:
            who = f"{namespace}/{who}"
        message = f"{op} {who} failed: {one_liner}"
        return SafeApiError(status, reason, message, hint=hint)

    # Non-apiserver exception. Use the class name, never the args.
    cls = type(e).__name__
    who = f"{kind}/{name}" if name else kind
    if namespace:
        who = f"{namespace}/{who}"
    return SafeApiError(
        status=0, reason=cls,
        message=f"{op} {who} failed: internal {cls}",
        hint="this is an internal error; check the server logs for details",
    )


# ===========================================================================
# Per-tool rate limit (P0-1)
# ===========================================================================
#
# In-memory token bucket, one bucket per tool name. Process-local state
# — restarts reset (matches the existing "MCP server restart clears
# in-memory caches" convention documented in user memory). The bucket
# is a leaky-bucket variant: each call consumes 1 token; tokens refill
# linearly at `refill_per_sec`; if the bucket is empty, calls are
# rejected with a `RateLimitedError` carrying `retry_after_seconds`.
#
# Deliberately NOT thread-safe at the lock level — Python's GIL makes
# the read/modify/write of `tokens` and `last_refill` atomic enough
# for a 120 RPM limit (worst-case race: 2–3 extra tokens consumed per
# window, not a correctness issue). If we later move tool dispatch to
# multiple threads (or async gather) we'll need a real `threading.Lock`.


class RateLimitedError(Exception):
    """Raised when a tool exceeds its per-tool rate limit."""

    def __init__(self, tool: str, retry_after_seconds: float, limit_rpm: int):
        self.tool = tool
        self.retry_after_seconds = float(retry_after_seconds)
        self.limit_rpm = int(limit_rpm)
        super().__init__(
            f"rate limit exceeded for {tool!r}: cap is {limit_rpm} RPM; "
            f"retry after {retry_after_seconds:.2f}s"
        )


class TokenBucket:
    """One bucket. Holds at most `capacity` tokens, refills at `refill_per_sec`."""

    __slots__ = ("capacity", "refill_per_sec", "tokens", "last_refill", "_lock")

    def __init__(self, capacity: int, refill_per_sec: float):
        self.capacity = int(capacity)
        self.refill_per_sec = float(refill_per_sec)
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()
        self._lock = threading.Lock()

    def try_consume(self, n: int = 1) -> tuple[bool, float]:
        """Try to take `n` tokens. Returns (ok, retry_after_seconds).

        When `ok=False`, `retry_after_seconds` is the time until the
        next token becomes available (i.e. how long the caller should
        wait before retrying).
        """
        with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            if elapsed > 0:
                self.tokens = min(
                    self.capacity,
                    self.tokens + elapsed * self.refill_per_sec,
                )
                self.last_refill = now
            if self.tokens >= n:
                self.tokens -= n
                return True, 0.0
            # Compute time until `n` tokens available.
            needed = n - self.tokens
            retry = needed / self.refill_per_sec if self.refill_per_sec > 0 else 60.0
            return False, retry


class RateLimiter:
    """One bucket per tool name. `rpm` = per-tool requests-per-minute cap."""

    def __init__(self, rpm: int):
        # rpm=0 disables the limiter entirely. The call_tool boundary
        # checks `self._rpm > 0` before calling check(); the math
        # below is skipped to keep disabled-mode truly zero-overhead.
        self._rpm = int(rpm)
        # Capacity = burst size = rpm/6 (10-second window worth of calls).
        # The 10s burst is a deliberate compromise: agents routinely fire
        # 5-10 quick follow-up calls (list → describe → logs) and we don't
        # want that to feel rate-limited, but a sustained 120 RPM
        # shouldn't drain 120 tokens in one second.
        self._capacity = max(1, self._rpm // 6) if self._rpm > 0 else 0
        self._refill_per_sec = self._rpm / 60.0 if self._rpm > 0 else 0.0
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def check(self, tool: str) -> None:
        """Raise `RateLimitedError` if the tool's bucket is empty. Else consume 1.

        No-op when the limiter is disabled (rpm=0). The call_tool
        boundary guards on `self._rpm > 0` before calling check(), but
        we double-belt here so the helper is safe to call directly in
        tests / future code paths.
        """
        if self._rpm <= 0:
            return
        with self._lock:
            bucket = self._buckets.get(tool)
            if bucket is None:
                bucket = TokenBucket(self._capacity, self._refill_per_sec)
                self._buckets[tool] = bucket
        ok, retry = bucket.try_consume()
        if not ok:
            raise RateLimitedError(
                tool, retry_after_seconds=retry, limit_rpm=self._rpm,
            )

    @property
    def rpm(self) -> int:
        return self._rpm


# ===========================================================================
# Per-call timeout (P0-2)
# ===========================================================================
#
# `asyncio.wait_for` is the natural fit, but the registered tool bodies
# are sync (they call kubernetes client methods synchronously). To make
# the boundary enforce a wall-clock cap we offload the tool call to the
# default executor and `wait_for` the resulting future. If the timeout
# fires the executor task is *not* cancelled — Python doesn't expose a
# portable way to kill a sync thread, and the kubernetes client is
# fully blocking — but the MCP request returns immediately with a
# `ToolTimeoutError` and the LLM can move on. The orphan task will complete
# (or hit its own apiserver timeout) in the background.
#
# `tool_timeout_s` defaults to 60s — generous for `list_*` / `describe`
# / `apply_yaml`; would need to be raised only for `rollout_status`
# with `watch=True` and the Prometheus range queries, both of which
# have their own per-tool timeout config the operator can override.

class ToolTimeoutError(Exception):
    """Raised when a tool call exceeds the configured per-call timeout."""

    def __init__(self, tool: str, timeout_seconds: float):
        self.tool = tool
        self.timeout_seconds = float(timeout_seconds)
        super().__init__(
            f"tool {tool!r} exceeded {timeout_seconds:g}s wall-clock cap; "
            f"the apiserver call may still be in flight in the background"
        )

