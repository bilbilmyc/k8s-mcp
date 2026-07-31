"""Tests for the operator-facing k8s-mcp CLI."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from k8s_mcp import __version__
from k8s_mcp.server import main


def test_help_is_available(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "doctor" in out
    assert "serve" in out


def test_version_is_available(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_doctor_is_redacted_by_default(capsys):
    with patch(
        "k8s_mcp.server.inspect_auth",
        return_value=("unavailable", "none", ["no credentials detected"]),
    ):
        main(["doctor"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["read_only"] is False
    assert payload["auth_mode"] == "unavailable"
    assert payload["auth_source"] == "none"
    assert payload["kubernetes_transport"]["read_timeout_s"] == 30
    assert payload["warnings"]
    assert "api_token" not in payload
