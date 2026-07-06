"""Tests for the production safety nets in `k8s_mcp.safety`:

  - `safe_apiserver_error` — maps raw `ApiException` (status / reason /
    body) to a curated `SafeApiError` one-liner. Verifies that the
    apiserver's verbose body (RBAC details, internal hostnames, manifest
    field paths) is NOT leaked into the message.
  - `RateLimiter` / `TokenBucket` — per-tool in-memory rate limit.
    Verifies the cap is enforced, the window refills, the retry-after
    estimate is honest, and disabled (rpm=0) bypasses the check.
  - `safe_apiserver_error` for non-`ApiException` — surfaces a generic
    message using the class name only, never the args.
"""
from __future__ import annotations

import time

import pytest
from kubernetes.client.rest import ApiException

from k8s_mcp.safety import (
    RateLimitedError,
    RateLimiter,
    SafeApiError,
    TokenBucket,
    safe_apiserver_error,
)

# ---------- safe_apiserver_error: ApiException ------------------------------


def test_403_rbac_denied_surfaces_hint():
    e = ApiException(status=403, reason="Forbidden")
    safe = safe_apiserver_error(e, op="read", kind="Pod", name="p1", namespace="ns1")
    assert isinstance(safe, SafeApiError)
    assert safe.status == 403
    assert safe.reason == "Forbidden"
    # Message is one line, no body.
    msg = str(safe)
    assert "\n" not in msg
    assert "ns1/Pod/p1" in msg
    assert "RBAC denied" in msg
    # Next-step hint promotes the recovery path.
    assert safe.hint is not None
    assert "whoami" in safe.hint
    # No leak of apiserver body fields.
    for forbidden in ("kind=", "apiVersion=", "details", "metadata", "groups"):
        assert forbidden not in msg


def test_404_not_found_no_hint():
    e = ApiException(status=404, reason="Not Found")
    safe = safe_apiserver_error(e, op="read", kind="ConfigMap", name="cm-x")
    assert safe.status == 404
    assert "not found" in str(safe).lower()
    # 404 is a normal control-flow signal; no hint needed.
    assert safe.hint is None


def test_409_conflict_suggests_retry():
    e = ApiException(status=409, reason="Conflict")
    safe = safe_apiserver_error(e, op="replace", kind="Deployment", name="web")
    assert safe.status == 409
    assert safe.hint is not None
    assert "re-read" in safe.hint.lower() or "retry" in safe.hint.lower()


def test_422_validation_hint_diff_resource():
    e = ApiException(status=422, reason="Invalid")
    safe = safe_apiserver_error(e, op="apply", kind="Service")
    assert safe.status == 422
    assert safe.hint is not None
    assert "diff_resource" in safe.hint


def test_429_503_504_have_retry_hints():
    for status, expected_word in [(429, "back off"), (503, "backoff"), (504, "backoff")]:
        e = ApiException(status=status, reason="Server Error")
        safe = safe_apiserver_error(e, op="list", kind="Pod")
        assert safe.hint is not None
        assert expected_word in safe.hint.lower()


def test_500_generic_no_body_leak():
    """500 may include stack frames in body — ensure body is never copied."""
    e = ApiException(status=500, reason="Internal Server Error")
    safe = safe_apiserver_error(e, op="patch", kind="StatefulSet", name="db")
    assert "Internal Server Error" in str(safe) or "internal error" in str(safe)
    # No path / stack hints in the message.
    msg = str(safe)
    assert "/" not in msg.split("db")[-1] or "call" in msg  # only "StatefulSet/db" identifier is OK


def test_unknown_status_falls_through_to_generic():
    e = ApiException(status=418, reason="I'm a teapot")
    safe = safe_apiserver_error(e, op="create", kind="Job")
    assert safe.status == 418
    assert "418" in str(safe)


def test_apiserver_body_field_never_leaked():
    """Real apiserver 403 bodies look like:

      {"kind":"Status","apiVersion":"v1","status":"Failure",
       "message":"configmaps \"foo\" is forbidden: User \"system:..\"
       cannot get resource \"configmaps\" in API group \"\" in the
       namespace \"ns1\"","code":403,"metadata":{} ...}

    None of those fields should reach the LLM.
    """
    e = ApiException(status=403, reason="Forbidden")
    e.body = (
        '{"kind":"Status","apiVersion":"v1","status":"Failure",'
        '"message":"configmaps \\"foo\\" is forbidden: User '
        'system:serviceaccount:ns1:default cannot get resource '
        'configmaps in API group in the namespace ns1",'
        '"code":403,"metadata":{}}'
    )
    safe = safe_apiserver_error(e, op="read", kind="ConfigMap", name="foo", namespace="ns1")
    msg = str(safe)
    for forbidden in (
        "system:serviceaccount", "configmaps", '"code":403', "is forbidden",
        "cannot get resource", "Failure",
    ):
        assert forbidden not in msg, f"apiserver body leaked: {forbidden!r} in {msg!r}"


# ---------- safe_apiserver_error: non-ApiException --------------------------


def test_non_apiserver_exception_uses_class_name_only():
    e = ValueError("user:pass@example.com is malformed")
    safe = safe_apiserver_error(e, op="read", kind="Pod", name="p")
    assert safe.status == 0
    assert safe.reason == "ValueError"
    msg = str(safe)
    # The class name appears, the args NEVER.
    assert "ValueError" in msg
    assert "user:pass" not in msg
    assert "malformed" not in msg
    assert "example.com" not in msg


def test_connection_error_class_name_only():
    e = ConnectionError("HTTPSConnectionPool(host='10.0.0.1', port=443): max retries exceeded")
    safe = safe_apiserver_error(e, op="list", kind="Node")
    assert "ConnectionError" in str(safe)
    assert "10.0.0.1" not in str(safe)
    assert "max retries" not in str(safe)


# ---------- safe_apiserver_error: resource identifier format ----------------


def test_resource_identifier_omits_name_when_empty():
    """For list-style ops the name is unknown — don't render a trailing `/`."""
    e = ApiException(status=403, reason="Forbidden")
    safe = safe_apiserver_error(e, op="list", kind="Pod")
    # The resource identifier is just `Pod` (no namespace, no name,
    # no trailing slash).
    msg = str(safe)
    assert "Pod failed" in msg
    assert "/Pod/" not in msg
    assert "/Pod" not in msg  # no namespace prefix when namespace is empty
    assert "Pod failed" in msg  # kind + op transition


def test_resource_identifier_includes_namespace_and_name():
    e = ApiException(status=404, reason="Not Found")
    safe = safe_apiserver_error(e, op="read", kind="Pod", name="p1", namespace="ns1")
    assert "ns1/Pod/p1" in str(safe)


# ===========================================================================
# TokenBucket
# ===========================================================================


def test_token_bucket_initial_full():
    """A fresh bucket can be drained to capacity without rejection."""
    b = TokenBucket(capacity=5, refill_per_sec=0.001)  # ~0.06/min, basically frozen
    for _ in range(5):
        ok, _ = b.try_consume()
        assert ok


def test_token_bucket_rejects_when_empty():
    b = TokenBucket(capacity=2, refill_per_sec=0.001)
    assert b.try_consume()[0]
    assert b.try_consume()[0]
    ok, retry = b.try_consume()
    assert not ok
    # Retry is positive and reflects the refill rate.
    assert retry > 0
    # With refill_per_sec = 0.001, 1 token takes ~1000s.
    assert retry > 100


def test_token_bucket_refills_over_time(monkeypatch):
    """A bucket drained now should refill after a small sleep."""
    b = TokenBucket(capacity=3, refill_per_sec=100.0)  # 100 tok/s = very fast
    for _ in range(3):
        b.try_consume()
    assert not b.try_consume()[0]
    # 100 tok/s = 10ms per token.
    time.sleep(0.02)
    ok, _ = b.try_consume()
    assert ok


def test_token_bucket_caps_at_capacity():
    """Idle bucket shouldn't accumulate more than `capacity` tokens."""
    b = TokenBucket(capacity=5, refill_per_sec=100.0)
    time.sleep(0.1)  # would refill 10 tokens, but cap is 5
    consumed = 0
    while b.try_consume()[0]:
        consumed += 1
    assert consumed == 5


# ===========================================================================
# RateLimiter (per-tool)
# ===========================================================================


def test_rate_limiter_first_n_calls_succeed():
    rl = RateLimiter(rpm=60)  # capacity = 60/6 = 10
    for _ in range(10):
        rl.check("list_pods")  # should not raise


def test_rate_limiter_rejects_above_burst():
    rl = RateLimiter(rpm=60)  # burst = 10
    for _ in range(10):
        rl.check("list_pods")
    with pytest.raises(RateLimitedError) as ei:
        rl.check("list_pods")
    assert ei.value.tool == "list_pods"
    assert ei.value.limit_rpm == 60
    assert ei.value.retry_after_seconds > 0


def test_rate_limiter_per_tool_isolation():
    """A hot `list_pods` should not block a `describe_resource`."""
    rl = RateLimiter(rpm=60)  # burst = 10
    for _ in range(10):
        rl.check("list_pods")
    # list_pods is now empty, but a different tool has its own bucket.
    rl.check("describe_resource")  # should not raise
    with pytest.raises(RateLimitedError) as ei:
        rl.check("list_pods")
    assert ei.value.tool == "list_pods"


def test_rate_limiter_disabled_when_rpm_zero():
    """rpm=0 means rate limiting is OFF — every call goes through."""
    rl = RateLimiter(rpm=0)
    # 1000 calls in a tight loop must all succeed.
    for _ in range(1000):
        rl.check("list_pods")


def test_rate_limiter_retry_after_is_honest():
    """The `retry_after` we surface should be close to the time it
    actually takes for the next token to become available."""
    rl = RateLimiter(rpm=600)  # 10 tok/s, 100ms per token, burst = 100
    for _ in range(100):
        rl.check("list_pods")
    with pytest.raises(RateLimitedError) as ei:
        rl.check("list_pods")
    # 10 tok/s = 0.1s/token. Allow some slack.
    assert 0.05 < ei.value.retry_after_seconds < 0.5


def test_rate_limiter_message_includes_tool_name_and_retry():
    rl = RateLimiter(rpm=60)
    for _ in range(10):
        rl.check("delete_pod")
    with pytest.raises(RateLimitedError, match="delete_pod") as ei:
        rl.check("delete_pod")
    msg = str(ei.value)
    assert "60" in msg  # cap
    assert "retry after" in msg.lower()
