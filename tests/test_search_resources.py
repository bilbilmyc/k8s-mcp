"""Tests for `search_resources` — cross-kind name substring search.

The tool fans out `_list_resource_rows` per kind and aggregates.
We monkeypatch `_list_resource_rows` to feed controlled lists so we
don't need a live cluster.
"""
from __future__ import annotations

import pytest
from kubernetes.client.rest import ApiException

from k8s_mcp.tools import generic


def _row(kind: str, name: str, namespace: str = "default"):
    return {"NAME": name, "NAMESPACE": namespace, "STATUS": "", "AGE": ""}


def test_empty_substring_raises():
    with pytest.raises(ValueError, match="non-empty"):
        generic.search_resources("")
    with pytest.raises(ValueError, match="non-empty"):
        generic.search_resources("   ")


def test_kinds_empty_list_raises():
    with pytest.raises(ValueError, match="non-empty"):
        generic.search_resources("foo", kinds=[])


def test_substring_match_across_kinds(monkeypatch):
    """Match 'web' across Pod / Service / ConfigMap; output table groups
    by KIND and includes the KIND column."""
    by_kind = {
        "Pod": [_row("Pod", "web-1"), _row("Pod", "db-1")],
        "Service": [_row("Service", "web-svc"), _row("Service", "db-svc")],
        "ConfigMap": [_row("ConfigMap", "web-config")],
    }

    def _fake_rows(kind, **kwargs):
        return by_kind.get(kind, [])

    monkeypatch.setattr(generic, "_list_resource_rows", _fake_rows)
    out = generic.search_resources("web", kinds=["Pod", "Service", "ConfigMap"])
    # Headers
    assert "KIND" in out
    assert "NAME" in out
    # Matches present
    assert "web-1" in out
    assert "web-svc" in out
    assert "web-config" in out
    # Non-matches absent
    assert "db-1" not in out
    assert "db-svc" not in out
    # Output is sorted by KIND then NAME — KINDs appear in alphabetical
    # order with the same KIND consecutive.
    assert out.index("ConfigMap") < out.index("Pod") < out.index("Service")


def test_substring_is_case_insensitive(monkeypatch):
    monkeypatch.setattr(
        generic, "_list_resource_rows",
        lambda kind, **kw: [_row(kind, "WEB-1")] if kind == "Pod" else [],
    )
    out = generic.search_resources("web", kinds=["Pod"])
    assert "WEB-1" in out


def test_namespace_filter_forwarded(monkeypatch):
    """namespace= is passed through to per-kind list, so cluster-wide
    queries don't accidentally surface unrelated namespaces."""
    captured: dict = {}

    def _fake_rows(kind, **kwargs):
        captured.setdefault(kind, []).append(kwargs.get("namespace"))
        return [_row(kind, f"{kind}-x")] if kind == "Pod" else []

    monkeypatch.setattr(generic, "_list_resource_rows", _fake_rows)
    generic.search_resources("x", kinds=["Pod"], namespace="app")
    assert captured["Pod"][0] == "app"


def test_label_selector_forwarded(monkeypatch):
    captured: dict = {}

    def _fake_rows(kind, **kwargs):
        captured[kind] = kwargs
        return []

    monkeypatch.setattr(generic, "_list_resource_rows", _fake_rows)
    generic.search_resources("x", kinds=["Pod"], label_selector="app=web")
    assert captured["Pod"].get("label_selector") == "app=web"


def test_limit_per_kind_forwarded(monkeypatch):
    captured: dict = {}

    def _fake_rows(kind, **kwargs):
        captured[kind] = kwargs
        return []

    monkeypatch.setattr(generic, "_list_resource_rows", _fake_rows)
    generic.search_resources("x", kinds=["Pod"], limit_per_kind=7)
    assert captured["Pod"].get("limit") == 7


def test_api_version_passed_per_kind(monkeypatch):
    """For CRDs in kinds=[...], api_versions={kind: av} must reach the
    underlying list call, otherwise the CRD can't be resolved."""
    captured: dict = {}

    def _fake_rows(kind, **kwargs):
        captured[kind] = kwargs
        return []

    monkeypatch.setattr(generic, "_list_resource_rows", _fake_rows)
    generic.search_resources(
        "x",
        kinds=["Certificate", "Pod"],
        api_versions={"Certificate": "cert-manager.io/v1"},
    )
    assert captured["Certificate"].get("api_version") == "cert-manager.io/v1"
    assert captured["Pod"].get("api_version") is None


def test_kinds_that_error_are_skipped_with_footer(monkeypatch):
    """Kinds that raise (RBAC forbidden / CRD not installed) are skipped,
    not propagated. Skipped count surfaces in the footer so the caller
    knows why a kind isn't in the result."""

    def _fake_rows(kind, **kwargs):
        if kind == "Deployment":
            raise ApiException(status=403, reason="forbidden")
        if kind == "IngressClass":
            raise ValueError("Unknown kind 'IngressClass'. Use get_api_resources")
        return [_row(kind, f"{kind}-x")]

    monkeypatch.setattr(generic, "_list_resource_rows", _fake_rows)
    out = generic.search_resources("x", kinds=["Pod", "Deployment", "IngressClass"])
    # The successful kind shows up
    assert "Pod-x" in out
    # Skipped count surfaces in footer
    assert "skipped" in out
    assert "Deployment" in out  # named in footer
    assert "IngressClass" in out  # named in footer


def test_no_match_returns_friendly_footer(monkeypatch):
    monkeypatch.setattr(generic, "_list_resource_rows", lambda kind, **kw: [])
    out = generic.search_resources("nothing-here", kinds=["Pod"])
    assert "no resources named like" in out
    assert "nothing-here" in out


def test_no_match_with_all_skipped_returns_friendly_footer(monkeypatch):
    """When every kind errored, still emit a useful footer naming them
    (otherwise the caller wonders why 'nothing-here' returned nothing)."""

    def _fake_rows(kind, **kwargs):
        raise ValueError(f"unknown kind {kind}")

    monkeypatch.setattr(generic, "_list_resource_rows", _fake_rows)
    out = generic.search_resources("nothing-here", kinds=["Pod", "Service"])
    assert "no resources named like" in out
    assert "skipped" in out or "errored" in out
    assert "Pod" in out  # named in footer
    assert "Service" in out  # named in footer


def test_default_kinds_searches_builtins(monkeypatch):
    """When kinds=None, the search hits built-in kinds via the default
    tuple. We can't enumerate the whole tuple here, but we can verify
    that a built-in kind is called when omitted."""
    captured: list[str] = []

    def _fake_rows(kind, **kwargs):
        captured.append(kind)
        return []

    monkeypatch.setattr(generic, "_list_resource_rows", _fake_rows)
    generic.search_resources("nothing")
    # At least Pod / Deployment / Service should be in the default list.
    for expected in ("Pod", "Deployment", "Service"):
        assert expected in captured, f"default kinds missing {expected}"


def test_parallel_path_used_for_many_kinds(monkeypatch):
    """With ≥ 5 kinds the worker must run on a ThreadPoolExecutor. We
    verify by checking observed peak concurrency > 1, the same way
    get_pod_logs tests do."""
    import threading
    import time

    sleep_s = 0.04
    captured_peak = {"n": 0, "peak": 0}
    lock = threading.Lock()

    def _fake_rows(kind, **kwargs):
        with lock:
            captured_peak["n"] += 1
            if captured_peak["n"] > captured_peak["peak"]:
                captured_peak["peak"] = captured_peak["n"]
        try:
            time.sleep(sleep_s)
            return []
        finally:
            with lock:
                captured_peak["n"] -= 1

    monkeypatch.setattr(generic, "_list_resource_rows", _fake_rows)
    kinds = ["Pod", "Service", "ConfigMap", "Secret", "Deployment"]
    start = time.monotonic()
    generic.search_resources("x", kinds=kinds)
    elapsed = time.monotonic() - start
    # Strictly faster than serial (5 × 0.04 = 0.20s) — leave CI noise room.
    assert elapsed < sleep_s * len(kinds) * 0.5
    # And we actually saw > 1 worker concurrent.
    assert captured_peak["peak"] > 1


def test_serial_path_used_for_few_kinds(monkeypatch):
    """Below the parallelism threshold we stay serial — keeps call
    order stable for any tests that grep output and avoids thread-pool
    overhead on the common 1-3 kind query."""
    import threading

    captured_peak = {"n": 0, "peak": 0}
    lock = threading.Lock()

    def _fake_rows(kind, **kwargs):
        with lock:
            captured_peak["n"] += 1
            if captured_peak["n"] > captured_peak["peak"]:
                captured_peak["peak"] = captured_peak["n"]
        try:
            import time as _t
            _t.sleep(0.005)
            return []
        finally:
            with lock:
                captured_peak["n"] -= 1

    monkeypatch.setattr(generic, "_list_resource_rows", _fake_rows)
    generic.search_resources("x", kinds=["Pod", "Service", "ConfigMap"])
    assert captured_peak["peak"] == 1
