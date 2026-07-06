"""Tests for caller-identity binding on destructive-op tokens.

A leaked HMAC-signed confirmation token must NOT be replayable from a
different MCP process running as a different kube identity. The token
payload is bound to `{"username", "uid"}` at issue time and re-checked
at verify time; mismatches raise TokenError.

The conftest autouse fixture pins `get_caller_identity` to
`{"username": "test-user", "uid": "test-uid", "groups": [...]}`. Tests
that need a different identity monkeypatch the helper directly.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from k8s_mcp import safety
from k8s_mcp.config import reset_settings_cache
from k8s_mcp.tools import bulk


# ---------------------------------------------------------------------------
# safety.make_delete_payload / assert_payload_matches — the binding contract
# ---------------------------------------------------------------------------


def test_make_delete_payload_includes_caller_when_provided():
    p = safety.make_delete_payload(
        "Pod", "p1", "default", 30,
        caller={"username": "alice", "uid": "u-1", "groups": []},
    )
    assert p["caller"] == {"username": "alice", "uid": "u-1"}


def test_make_delete_payload_omits_caller_when_none():
    p = safety.make_delete_payload("Pod", "p1", "default", 30, caller=None)
    assert "caller" not in p


def test_assert_payload_matches_accepts_matching_caller():
    payload = safety.make_delete_payload(
        "Pod", "p1", "default", 30,
        caller={"username": "alice", "uid": "u-1", "groups": []},
    )
    safety.assert_payload_matches(
        payload,
        kind="Pod", name="p1", namespace="default", grace_period_seconds=30,
        caller={"username": "alice", "uid": "u-1", "groups": []},
    )


def test_assert_payload_matches_rejects_different_username():
    payload = safety.make_delete_payload(
        "Pod", "p1", "default", 30,
        caller={"username": "alice", "uid": "u-1", "groups": []},
    )
    with pytest.raises(safety.TokenError, match="caller mismatch"):
        safety.assert_payload_matches(
            payload,
            kind="Pod", name="p1", namespace="default", grace_period_seconds=30,
            caller={"username": "bob", "uid": "u-2", "groups": []},
        )


def test_assert_payload_matches_rejects_different_uid_same_username():
    """Username collision (rare but possible across clusters/renames) is
    caught by the UID defense-in-depth check."""
    payload = safety.make_delete_payload(
        "Pod", "p1", "default", 30,
        caller={"username": "alice", "uid": "u-1", "groups": []},
    )
    with pytest.raises(safety.TokenError, match="UID mismatch"):
        safety.assert_payload_matches(
            payload,
            kind="Pod", name="p1", namespace="default", grace_period_seconds=30,
            caller={"username": "alice", "uid": "u-2", "groups": []},
        )


def test_assert_payload_matches_skips_caller_check_when_no_caller_on_call():
    """Operator bypass for legacy callers — same shape as the old API.
    NOT recommended, but documented."""
    payload = safety.make_delete_payload(
        "Pod", "p1", "default", 30,
        caller={"username": "alice", "uid": "u-1", "groups": []},
    )
    safety.assert_payload_matches(
        payload,
        kind="Pod", name="p1", namespace="default", grace_period_seconds=30,
        caller=None,
    )  # does not raise


def test_assert_payload_matches_rejects_legacy_token_against_bound_caller():
    """A token issued without a caller field (older code path) cannot
    be replayed once the caller binding is enforced. The verify side
    checks `token_caller` against the live caller's username; if the
    token has no caller and the live side has one, the lookup short-
    circuits to '' != 'test-user' → reject."""
    payload = safety.make_delete_payload("Pod", "p1", "default", 30)
    assert "caller" not in payload
    with pytest.raises(safety.TokenError, match="caller mismatch"):
        safety.assert_payload_matches(
            payload,
            kind="Pod", name="p1", namespace="default", grace_period_seconds=30,
            caller={"username": "test-user", "uid": "test-uid", "groups": []},
        )


# ---------------------------------------------------------------------------
# bulk._verify_bulk_token — the same binding enforced on the bulk path
# ---------------------------------------------------------------------------


def test_bulk_token_roundtrip_with_same_identity(monkeypatch):
    """Issue + verify with the same identity (the common case)."""
    monkeypatch.setattr(
        "k8s_mcp.client.get_caller_identity",
        lambda: {"username": "alice", "uid": "u-1", "groups": []},
    )
    monkeypatch.setattr(
        "k8s_mcp.tools.bulk.get_caller_identity",
        lambda: {"username": "alice", "uid": "u-1", "groups": []},
    )
    token = bulk._issue_bulk_token({"op": "bulk_restart", "label_selector": "app=x"})
    payload = bulk._verify_bulk_token(token, expected_op="bulk_restart")
    assert payload["op"] == "bulk_restart"
    assert payload["caller"] == {"username": "alice", "uid": "u-1"}


def test_bulk_token_rejected_when_caller_changes(monkeypatch):
    """A token issued as alice cannot be replayed by a process now
    running as bob — even if the secret is the same."""
    alice = lambda: {"username": "alice", "uid": "u-1", "groups": []}
    bob = lambda: {"username": "bob", "uid": "u-2", "groups": []}

    monkeypatch.setattr("k8s_mcp.client.get_caller_identity", alice)
    monkeypatch.setattr("k8s_mcp.tools.bulk.get_caller_identity", alice)
    token = bulk._issue_bulk_token({"op": "bulk_restart"})

    # Simulate process-rotation (e.g. kubeconfig reload to a different SA).
    monkeypatch.setattr("k8s_mcp.client.get_caller_identity", bob)
    monkeypatch.setattr("k8s_mcp.tools.bulk.get_caller_identity", bob)
    with pytest.raises(safety.TokenError, match="caller mismatch"):
        bulk._verify_bulk_token(token, expected_op="bulk_restart")


# ---------------------------------------------------------------------------
# end-to-end: a leaked token cannot traverse two MCP processes
# ---------------------------------------------------------------------------


def test_leaked_token_replay_across_identities_fails(monkeypatch):
    """Simulate a token stolen from process-A (running as alice) being
    presented to process-B (running as bob). Even if bob knows the
    HMAC secret (e.g. via env leak), the caller binding refuses."""
    from k8s_mcp.tools import delete_tool

    alice = lambda: {"username": "alice", "uid": "u-1", "groups": []}
    bob = lambda: {"username": "bob", "uid": "u-2", "groups": []}

    # Process-A issues the token.
    monkeypatch.setattr("k8s_mcp.client.get_caller_identity", alice)
    monkeypatch.setattr("k8s_mcp.tools.delete_tool.get_caller_identity", alice)
    token = safety.make_delete_payload(
        "Pod", "p1", "default", 30, caller=alice()
    )
    signed = safety.issue_token(token, secret="test-secret-not-change-me", ttl_seconds=300)

    # Process-B verifies — different identity, refuses.
    monkeypatch.setattr("k8s_mcp.client.get_caller_identity", bob)
    monkeypatch.setattr("k8s_mcp.tools.delete_tool.get_caller_identity", bob)
    payload = safety.verify_token(signed, secret="test-secret-not-change-me")
    with pytest.raises(safety.TokenError, match="caller mismatch"):
        safety.assert_payload_matches(
            payload,
            kind="Pod", name="p1", namespace="default", grace_period_seconds=30,
            caller=bob(),
        )