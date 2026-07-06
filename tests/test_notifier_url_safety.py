"""Tests for notifier URL safety gate.

The notifier must refuse:
  - http:// URLs by default (cleartext leak risk)
  - non-http(s) schemes (file://, ftp://, gopher://, etc.)
  - hosts outside K8S_MCP_NOTIFIER_URL_ALLOWLIST when set
And must accept:
  - https URLs to any host (default)
  - http URLs when K8S_MCP_NOTIFIER_URL_ALLOW_HTTP=true
  - https URLs to allowlisted hosts when NOTIFIER_URL_ALLOWLIST is set
"""
from __future__ import annotations

import pytest

from k8s_mcp.config import reset_settings_cache
from k8s_mcp.tools import notifier


@pytest.fixture(autouse=True)
def _clear():
    reset_settings_cache()
    yield
    reset_settings_cache()


# ---------------------------------------------------------------------------
# _validate_notifier_url — pure function over (url, settings)
# ---------------------------------------------------------------------------


def test_https_url_accepted_by_default():
    assert notifier._validate_notifier_url("https://hooks.slack.com/svc") is None


def test_http_url_refused_by_default():
    err = notifier._validate_notifier_url("http://internal:8080/hook")
    assert err is not None
    assert "scheme 'http' refused" in err
    assert "K8S_MCP_NOTIFIER_URL_ALLOW_HTTP" in err


def test_http_url_accepted_when_allow_http_true(monkeypatch):
    monkeypatch.setenv("K8S_MCP_NOTIFIER_URL_ALLOW_HTTP", "true")
    reset_settings_cache()
    assert notifier._validate_notifier_url("http://localhost:8080/hook") is None


def test_file_scheme_refused():
    err = notifier._validate_notifier_url("file:///etc/passwd")
    assert err is not None
    assert "scheme 'file'" in err


def test_gopher_scheme_refused():
    err = notifier._validate_notifier_url("gopher://evil/")
    assert err is not None
    assert "scheme 'gopher'" in err


def test_javascript_scheme_refused():
    err = notifier._validate_notifier_url("javascript:alert(1)")
    assert err is not None


def test_empty_url_refused():
    err = notifier._validate_notifier_url("")
    assert err is not None


def test_url_without_host_refused():
    err = notifier._validate_notifier_url("https:///path-only")
    assert err is not None
    assert "no host" in err


def test_allowlist_accepts_matching_host(monkeypatch):
    monkeypatch.setenv(
        "K8S_MCP_NOTIFIER_URL_ALLOWLIST", "open.feishu.cn,hooks.slack.com"
    )
    reset_settings_cache()
    assert (
        notifier._validate_notifier_url("https://open.feishu.cn/open-apis/bot/v2/hook/abc")
        is None
    )
    assert (
        notifier._validate_notifier_url("https://hooks.slack.com/services/x/y/z")
        is None
    )


def test_allowlist_refuses_non_matching_host(monkeypatch):
    monkeypatch.setenv("K8S_MCP_NOTIFIER_URL_ALLOWLIST", "open.feishu.cn")
    reset_settings_cache()
    err = notifier._validate_notifier_url("https://evil.example.com/hook")
    assert err is not None
    assert "evil.example.com" in err
    assert "K8S_MCP_NOTIFIER_URL_ALLOWLIST" in err


def test_allowlist_is_exact_match_no_subdomain(monkeypatch):
    """Subdomain wildcards are NOT supported — `open.feishu.cn` does not
    match `api.open.feishu.cn`. Pin the policy."""
    monkeypatch.setenv("K8S_MCP_NOTIFIER_URL_ALLOWLIST", "open.feishu.cn")
    reset_settings_cache()
    err = notifier._validate_notifier_url("https://api.open.feishu.cn/hook")
    assert err is not None


def test_allowlist_host_match_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("K8S_MCP_NOTIFIER_URL_ALLOWLIST", "Open.Feishu.cn")
    reset_settings_cache()
    assert (
        notifier._validate_notifier_url("https://open.feishu.cn/hook") is None
    )


# ---------------------------------------------------------------------------
# _validate_notifier — surfaces the URL error in the notifier error message
# ---------------------------------------------------------------------------


def test_notifier_with_http_url_is_rejected_as_invalid():
    n = {"name": "ops", "type": "slack", "url": "http://internal/hook"}
    err = notifier._validate_notifier(n, 0)
    assert err is not None
    assert "http" in err
    assert "ops" in err


def test_notifier_with_https_url_passes():
    n = {"name": "ops", "type": "slack", "url": "https://hooks.slack.com/svc"}
    assert notifier._validate_notifier(n, 0) is None


def test_notifier_with_file_scheme_rejected():
    n = {"name": "ops", "type": "slack", "url": "file:///etc/passwd"}
    err = notifier._validate_notifier(n, 0)
    assert err is not None
    assert "scheme 'file'" in err
