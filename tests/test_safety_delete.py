"""Tests for the confirmation-token machinery and the delete_tool guards."""
from __future__ import annotations

import pytest

from k8s_mcp import safety
from k8s_mcp.config import reset_settings_cache

# ---- safety.issue_token / verify_token ---------------------------------------


def test_roundtrip():
    payload = {"op": "delete", "kind": "Pod", "name": "x"}
    token = safety.issue_token(payload, secret="s3cret", ttl_seconds=60)
    decoded = safety.verify_token(token, secret="s3cret")
    assert decoded["op"] == "delete"
    assert decoded["kind"] == "Pod"
    assert decoded["name"] == "x"
    assert "exp" in decoded


def test_wrong_secret_rejected():
    token = safety.issue_token({"x": 1}, secret="real", ttl_seconds=60)
    with pytest.raises(safety.TokenError, match="Invalid confirmation_token"):
        safety.verify_token(token, secret="other")


def test_expired_rejected():
    token = safety.issue_token({"x": 1}, secret="s", ttl_seconds=-10)
    with pytest.raises(safety.TokenError, match="expired"):
        safety.verify_token(token, secret="s")


def test_malformed_rejected():
    with pytest.raises(safety.TokenError, match="Malformed"):
        safety.verify_token("not-a-token", secret="s")


def test_tampered_body_rejected():
    token = safety.issue_token({"x": 1}, secret="s", ttl_seconds=60)
    body, sig = token.split(".")
    # flip one character of the body to break signature
    tampered = ("A" if body[0] != "A" else "B") + body[1:]
    bad = f"{tampered}.{sig}"
    with pytest.raises(safety.TokenError, match="Invalid confirmation_token"):
        safety.verify_token(bad, secret="s")


def test_empty_secret_raises_on_issue():
    with pytest.raises(safety.TokenError, match="not configured"):
        safety.issue_token({"x": 1}, secret="", ttl_seconds=60)


def test_empty_secret_raises_on_verify():
    token = safety.issue_token({"x": 1}, secret="s", ttl_seconds=60)
    with pytest.raises(safety.TokenError, match="not configured"):
        safety.verify_token(token, secret="")


def test_payload_matching_succeeds():
    payload = safety.make_delete_payload("Deployment", "web", "default", 30)
    safety.assert_payload_matches(
        payload, kind="Deployment", name="web", namespace="default", grace_period_seconds=30
    )


def test_payload_mismatch_kind():
    payload = safety.make_delete_payload("Deployment", "web", "default", 30)
    with pytest.raises(safety.TokenError, match="kind mismatch"):
        safety.assert_payload_matches(
            payload, kind="StatefulSet", name="web", namespace="default", grace_period_seconds=30
        )


def test_payload_mismatch_namespace():
    payload = safety.make_delete_payload("Pod", "x", "default", 30)
    with pytest.raises(safety.TokenError, match="namespace mismatch"):
        safety.assert_payload_matches(
            payload, kind="Pod", name="x", namespace="other", grace_period_seconds=30
        )


def test_payload_mismatch_grace_period():
    payload = safety.make_delete_payload("Pod", "x", None, 30)
    with pytest.raises(safety.TokenError, match="grace_period mismatch"):
        safety.assert_payload_matches(
            payload, kind="Pod", name="x", namespace=None, grace_period_seconds=0
        )


def test_payload_mismatch_op():
    payload = {"op": "scale", "kind": "Pod", "name": "x", "namespace": "", "grace_period_seconds": 30}
    with pytest.raises(safety.TokenError, match="not issued for a delete"):
        safety.assert_payload_matches(
            payload, kind="Pod", name="x", namespace=None, grace_period_seconds=30
        )


# ---- delete_tool guards -----------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_settings_cache()
    yield
    reset_settings_cache()


def test_delete_read_only_rejected(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "true")
    reset_settings_cache()
    from k8s_mcp.tools import delete_tool
    with pytest.raises(PermissionError, match="read-only"):
        delete_tool.delete_resource(kind="Pod", name="x", namespace="default")


def test_delete_blocked_by_allowlist(monkeypatch):
    monkeypatch.setenv("K8S_MCP_NAMESPACE_ALLOWLIST", "allowed")
    reset_settings_cache()
    from k8s_mcp.tools import delete_tool
    with pytest.raises(PermissionError, match="not allowed"):
        delete_tool.delete_resource(kind="Pod", name="x", namespace="other")


def test_delete_confirm_true_without_token_rejected(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()
    from k8s_mcp.tools import delete_tool
    with pytest.raises(safety.TokenError, match="requires the confirmation_token"):
        delete_tool.delete_resource(
            kind="Pod", name="x", namespace="default",
            confirm=True, confirmation_token=None,
        )


def test_delete_confirm_true_with_bad_token_rejected(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    monkeypatch.setenv("K8S_MCP_DELETE_TOKEN_SECRET", "real-secret")
    reset_settings_cache()
    from k8s_mcp.tools import delete_tool
    with pytest.raises(safety.TokenError):
        delete_tool.delete_resource(
            kind="Pod", name="x", namespace="default",
            confirm=True, confirmation_token="bad.token",
        )


def test_delete_preview_returns_token(monkeypatch):
    """The preview path returns a token; we mock the cluster fetch."""
    from unittest.mock import patch

    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    monkeypatch.setenv("K8S_MCP_DELETE_TOKEN_SECRET", "real-secret")
    reset_settings_cache()

    from k8s_mcp.tools import delete_tool

    fake_obj = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "x", "namespace": "default"}, "spec": {}}
    with patch.object(delete_tool, "_fetch", return_value=fake_obj):
        result = delete_tool.delete_resource(
            kind="Pod", name="x", namespace="default", confirm=False,
        )

    assert "preview_yaml" in result
    assert "confirmation_token" in result
    assert result["expires_in_seconds"] > 0
    assert "instruction" in result
    # Token should be valid
    payload = safety.verify_token(result["confirmation_token"], "real-secret")
    assert payload["kind"] == "Pod"
    assert payload["name"] == "x"
    assert payload["namespace"] == "default"


# =============================================================================
# assert_caller_matches — shared caller-binding check used by bulk + storage
# =============================================================================


def test_assert_caller_matches_passes_when_identical():
    safety.assert_caller_matches(
        {"username": "alice", "uid": "u-1"},
        {"username": "alice", "uid": "u-1"},
    )


def test_assert_caller_matches_rejects_username_mismatch():
    with pytest.raises(safety.TokenError, match="caller mismatch"):
        safety.assert_caller_matches(
            {"username": "alice", "uid": "u-1"},
            {"username": "mallory", "uid": "u-1"},
        )


def test_assert_caller_matches_rejects_uid_mismatch():
    """Same username, different UID — username can collide across
    SAs/namespaces, but UID is the stable identifier. Defense-in-depth
    against token replay across distinct ServiceAccounts."""
    with pytest.raises(safety.TokenError, match="UID mismatch"):
        safety.assert_caller_matches(
            {"username": "alice", "uid": "u-1"},
            {"username": "alice", "uid": "u-2"},
        )


def test_assert_caller_matches_treats_none_token_caller_as_empty():
    """A token with no embedded caller dict (older payload or hand-rolled
    token) must be rejected — never silently accepted."""
    with pytest.raises(safety.TokenError):
        safety.assert_caller_matches(
            None,
            {"username": "alice", "uid": "u-1"},
        )
