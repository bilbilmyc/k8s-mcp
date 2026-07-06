"""Tests for the audit-log emission on get_secret_value(reveal=True).

The audit log line is what SOC has to grep when investigating a leaked
secret — the rule is: every successful reveal MUST emit one INFO line
with secret name + namespace + key, and the actual bytes MUST NEVER
appear in the log.
"""
from __future__ import annotations

import base64
import logging

import pytest

from k8s_mcp.config import reset_settings_cache
from k8s_mcp.tools import secret


@pytest.fixture(autouse=True)
def _clear():
    reset_settings_cache()
    yield
    reset_settings_cache()


class _FakeSecret:
    def __init__(self, data, string_data=None):
        self.data = data
        self.string_data = string_data or {}

    def to_dict(self):
        out: dict = {"data": self.data}
        if self.string_data:
            out["stringData"] = self.string_data
        return out


class _FakeItem:
    def __init__(self, secret_obj):
        self._s = secret_obj

    def to_dict(self):
        return self._s.to_dict()


class _FakeResource:
    def __init__(self, secret_obj):
        self._s = secret_obj

    def get(self, **kw):
        return _FakeItem(self._s)


class _R:
    def __init__(self, secret_obj):
        self._s = secret_obj

    def get(self, **kw):
        return _FakeResource(self._s)


class _D:
    def __init__(self, secret_obj):
        self._s = secret_obj

    @property
    def resources(self):
        return _R(self._s)


def test_reveal_true_logs_audit_line_with_metadata(monkeypatch, caplog):
    fake = _FakeSecret({"password": base64.b64encode(b"hunter2").decode()})
    monkeypatch.setattr(secret, "_dyn", lambda: _D(fake))
    with caplog.at_level(logging.INFO, logger="k8s_mcp.tools.secret"):
        out = secret.get_secret_value("db", "default", "password", reveal=True)
    assert out == "hunter2"
    audit_lines = [r for r in caplog.records if r.message.startswith("secret_reveal")]
    assert len(audit_lines) == 1
    msg = audit_lines[0].message
    assert "name=db" in msg
    assert "namespace=default" in msg
    assert "key=password" in msg


def test_reveal_true_audit_does_not_leak_value(monkeypatch, caplog):
    """The audit log records metadata, NEVER the actual secret bytes."""
    fake = _FakeSecret({"password": base64.b64encode(b"super-secret-do-not-log").decode()})
    monkeypatch.setattr(secret, "_dyn", lambda: _D(fake))
    with caplog.at_level(logging.INFO, logger="k8s_mcp.tools.secret"):
        secret.get_secret_value("db", "default", "password", reveal=True)
    full_log = "\n".join(r.getMessage() for r in caplog.records)
    assert "super-secret-do-not-log" not in full_log
    assert "hunter2" not in full_log


def test_reveal_false_does_not_log_audit_line(monkeypatch, caplog):
    """`reveal=False` is metadata-only and shouldn't pollute the audit log."""
    fake = _FakeSecret({"password": base64.b64encode(b"hunter2").decode()})
    monkeypatch.setattr(secret, "_dyn", lambda: _D(fake))
    with caplog.at_level(logging.INFO, logger="k8s_mcp.tools.secret"):
        out = secret.get_secret_value("db", "default", "password", reveal=False)
    assert "MASKED" in out.upper() or "***" in out
    audit_lines = [r for r in caplog.records if r.message.startswith("secret_reveal")]
    assert audit_lines == []


def test_reveal_true_log_level_is_info_not_debug(monkeypatch, caplog):
    """The audit line must show up at default log level (INFO), not just DEBUG."""
    fake = _FakeSecret({"password": base64.b64encode(b"x").decode()})
    monkeypatch.setattr(secret, "_dyn", lambda: _D(fake))
    with caplog.at_level(logging.INFO):  # INFO threshold, no DEBUG
        secret.get_secret_value("db", "default", "password", reveal=True)
    levels = [r.levelno for r in caplog.records if r.message.startswith("secret_reveal")]
    assert logging.INFO in levels


def test_reveal_true_logs_string_data_audit_line(monkeypatch, caplog):
    """stringData path also emits the audit line (not just data)."""
    fake = _FakeSecret({}, string_data={"plain": "hello"})
    monkeypatch.setattr(secret, "_dyn", lambda: _D(fake))
    with caplog.at_level(logging.INFO, logger="k8s_mcp.tools.secret"):
        secret.get_secret_value("db", "default", "plain", reveal=True)
    audit_lines = [r for r in caplog.records if r.message.startswith("secret_reveal")]
    assert len(audit_lines) == 1
    assert "key=plain" in audit_lines[0].message
