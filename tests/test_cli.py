"""Tests for the operator-facing k8s-mcp CLI."""
from __future__ import annotations

import json

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
    main(["doctor"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["read_only"] is False
    assert payload["auth_mode"] == "auto_detect"
    assert "api_token" not in payload
