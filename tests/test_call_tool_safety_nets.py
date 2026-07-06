"""Integration tests for the call_tool boundary in `k8s_mcp.server`.

Verifies the three production safety nets are actually wired into the
`_K8sMCP.call_tool` override — not just sitting in `safety.py` as
unused helpers. We drive `_K8sMCP` with a fake tool that simulates
the three failure modes (ApiException / generic exception / sleep).

These tests do NOT need an apiserver — they build a real `_K8sMCP`
instance and call `await mcp.call_tool(name, {})` directly.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from kubernetes.client.rest import ApiException

from k8s_mcp.config import Settings
from k8s_mcp.safety import RateLimitedError, SafeApiError, ToolTimeoutError
from k8s_mcp.server import _K8sMCP

# ---------- test fixtures --------------------------------------------------


def _build_server(*, rate_limit_rpm: int = 120, tool_timeout_s: float = 5.0) -> _K8sMCP:
    """Build a _K8sMCP with explicit safety-net settings."""
    # Bypass the env / lru_cache by passing a Settings directly. The
    # `Settings` model is the only kwarg the server cares about.
    settings = Settings(
        read_only=True,  # avoid the enforce_write_safety token check
        rate_limit_rpm=rate_limit_rpm,
        tool_timeout_s=tool_timeout_s,
    )
    return _K8sMCP("k8s-mcp-test", settings=settings)


def _add_simple_tool(server: _K8sMCP, name: str, fn) -> None:
    """Register a sync tool body. The server's call_tool override will
    wrap it with rate-limit / timeout / safe-error handling."""
    server.add_tool(fn, name=name, description=f"test tool {name}")


# ---------- P0-1: rate limit enforced at the boundary ----------------------


def test_rate_limit_rejects_burst_at_boundary():
    """When a tool is called > burst times in a window, call_tool
    raises RateLimitedError and the tool body is NOT invoked."""
    server = _build_server(rate_limit_rpm=60)  # burst = 10
    invocations = []

    def hot_tool() -> str:
        invocations.append(time.monotonic())
        return "ok"

    _add_simple_tool(server, "hot_tool", hot_tool)
    # Burn the bucket.
    for _ in range(10):
        asyncio.run(server.call_tool("hot_tool", {}))
    # 11th call — must raise RateLimitedError, tool body NOT called.
    with pytest.raises(RateLimitedError) as ei:
        asyncio.run(server.call_tool("hot_tool", {}))
    assert ei.value.tool == "hot_tool"
    assert len(invocations) == 10  # tool body wasn't entered the 11th time


def test_rate_limit_disabled_when_rpm_zero():
    server = _build_server(rate_limit_rpm=0)
    _add_simple_tool(server, "x", lambda: "ok")
    # 100 calls in a tight loop must all succeed (no rate limit).
    for _ in range(100):
        result = asyncio.run(server.call_tool("x", {}))
        assert result[0].text == "ok"  # MCP wraps string in TextContent


# ---------- P0-2: per-call timeout enforced at the boundary -----------------


def test_tool_timeout_raises_error():
    """A tool that sleeps > timeout must raise ToolTimeoutError — the wall
    clock caps the call, the tool body is left to complete in the
    background (and that's fine).

    Note on timing: the call_tool boundary returns within the timeout
    (proved by the boundary log + the next test case). But the
    `asyncio.run(...)` wrapper used in this test waits for ALL
    non-daemon threads to finish on event-loop shutdown — the orphan
    executor thread running the slow `time.sleep` is one of those.
    So the wall clock measured here is the orphan's lifetime, not
    the boundary's response time. In the real long-lived MCP server
    the boundary returns immediately and the orphan is left
    to complete (or hit the apiserver's own timeout) on its own.
    """
    server = _build_server(tool_timeout_s=0.2)

    def slow_tool() -> str:
        time.sleep(2.0)  # 10x the timeout
        return "should not see this"

    _add_simple_tool(server, "slow_tool", slow_tool)
    with pytest.raises(ToolTimeoutError) as ei:
        asyncio.run(server.call_tool("slow_tool", {}))
    assert ei.value.tool == "slow_tool"
    assert ei.value.timeout_seconds == 0.2


def test_tool_timeout_boundary_returns_within_timeout():
    """The call_tool boundary itself must return within the timeout
    — independent of how long the orphan executor thread takes to
    finish. We measure this on a long-lived event loop (mimicking
    the real `mcp.run()` server) where asyncio.run's
    thread-shutdown wait doesn't mask the timing.
    """
    server = _build_server(tool_timeout_s=0.2)

    def slow_tool() -> str:
        time.sleep(2.0)
        return "should not see this"

    _add_simple_tool(server, "slow_tool", slow_tool)

    async def _drive():
        return await server.call_tool("slow_tool", {})

    async def _runner():
        task = asyncio.create_task(_drive())
        start = time.monotonic()
        with pytest.raises(ToolTimeoutError):
            await task
        return time.monotonic() - start

    elapsed = asyncio.run(_runner())
    assert elapsed < 1.0, f"boundary took {elapsed:.2f}s, expected < 1.0s"


def test_tool_timeout_disabled_when_zero():
    """TOOL_TIMEOUT_S=0 means no cap — a slow tool runs to completion."""
    server = _build_server(tool_timeout_s=0)
    _add_simple_tool(server, "slow_tool", lambda: time.sleep(0.1) or "ok")
    result = asyncio.run(server.call_tool("slow_tool", {}))
    # MCP returns ContentBlock list; the text is what the tool returned.
    assert result[0].text == "ok"


# ---------- P1-4: ApiException sanitized at the boundary --------------------


def test_apiserver_403_sanitized_to_safe_error():
    """An ApiException from the tool body must be replaced with a
    SafeApiError before it crosses the call_tool boundary — the LLM
    must never see the raw apiserver body."""
    server = _build_server()

    def raises_403() -> str:
        e = ApiException(status=403, reason="Forbidden")
        e.body = (
            '{"kind":"Status","apiVersion":"v1","status":"Failure",'
            '"message":"secrets foo is forbidden: User '
            'system:serviceaccount:ns1:default cannot get resource '
            'secrets in the namespace ns1","code":403}'
        )
        raise e

    _add_simple_tool(server, "raises_403", raises_403)
    with pytest.raises(SafeApiError) as ei:
        asyncio.run(server.call_tool("raises_403", {}))
    safe = ei.value
    assert safe.status == 403
    msg = str(safe)
    # One-liner; no apiserver body fields.
    for forbidden in (
        "system:serviceaccount", "secrets foo", "cannot get resource",
        '"code":403', "Failure", "is forbidden",
    ):
        assert forbidden not in msg, f"apiserver body leaked: {forbidden!r} in {msg!r}"
    # Next-step hint surfaces.
    assert safe.hint is not None
    assert "whoami" in safe.hint


def test_apiserver_404_sanitized():
    server = _build_server()

    def raises_404() -> str:
        raise ApiException(status=404, reason="Not Found")

    _add_simple_tool(server, "raises_404", raises_404)
    with pytest.raises(SafeApiError) as ei:
        asyncio.run(server.call_tool("raises_404", {}))
    assert ei.value.status == 404
    assert "not found" in str(ei.value).lower()


def test_generic_exception_sanitized_to_class_name():
    """A non-ApiException failure (e.g. KeyError) must not leak the
    exception args — only the class name."""
    server = _build_server()

    def raises_value_error() -> str:
        raise ValueError("internal: secret-token=abc123 leaked here")

    _add_simple_tool(server, "raises_value_error", raises_value_error)
    with pytest.raises(SafeApiError) as ei:
        asyncio.run(server.call_tool("raises_value_error", {}))
    msg = str(ei.value)
    assert "ValueError" in msg
    # Args must NOT appear in the LLM-facing message.
    assert "secret-token" not in msg
    assert "abc123" not in msg
    assert "leaked here" not in msg


# ---------- happy path: tool that returns a value still works ---------------


def test_normal_tool_call_unaffected():
    server = _build_server()
    _add_simple_tool(server, "echo", lambda: "hello world")
    result = asyncio.run(server.call_tool("echo", {}))
    # FastMCP returns a sequence of ContentBlock; for a `-> str` tool
    # with structured_output=False it's a single TextContent.
    assert len(result) == 1
    assert result[0].text == "hello world"


def test_tool_with_arguments_passes_through():
    server = _build_server()
    _add_simple_tool(server, "greet", lambda name: f"hi {name}")
    result = asyncio.run(server.call_tool("greet", {"name": "alice"}))
    assert result[0].text == "hi alice"
