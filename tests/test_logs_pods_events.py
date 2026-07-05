"""Tests for formatters.format_age / format_relative_time.

These helpers replaced the per-module `_age` / `_rel_time` / `_format_time`
duplicates. The CoreV1Api call paths require a live cluster; we only test
the pure helpers here.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from k8s_mcp.formatters import format_age, format_relative_time


def test_age_seconds():
    ts = datetime.now(UTC) - timedelta(seconds=30)
    assert format_age(ts) == "30s"


def test_age_minutes():
    ts = datetime.now(UTC) - timedelta(minutes=5)
    assert format_age(ts) == "5m"


def test_age_hours():
    ts = datetime.now(UTC) - timedelta(hours=3)
    assert format_age(ts) == "3h"


def test_age_days():
    ts = datetime.now(UTC) - timedelta(days=2)
    assert format_age(ts) == "2d"


def test_age_none():
    assert format_age(None) == ""


def test_age_string_iso():
    ts = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    assert format_age(ts) == "10s"


def test_format_time_seconds_ago():
    ts = datetime.now(UTC) - timedelta(seconds=10)
    assert format_relative_time(ts) == "10s ago"


def test_format_time_minutes_ago():
    ts = datetime.now(UTC) - timedelta(minutes=3)
    assert format_relative_time(ts) == "3m ago"


def test_format_time_hours_ago():
    ts = datetime.now(UTC) - timedelta(hours=2)
    assert format_relative_time(ts) == "2h ago"


def test_format_time_none():
    assert format_relative_time(None) == ""


def test_format_time_string_iso():
    ts = (datetime.now(UTC) - timedelta(seconds=15)).isoformat()
    assert format_relative_time(ts) == "15s ago"


def test_format_time_naive_iso_treated_as_utc():
    ts = (datetime.now(UTC) - timedelta(seconds=20)).replace(tzinfo=None).isoformat()
    out = format_relative_time(ts)
    assert out.endswith("s ago")
