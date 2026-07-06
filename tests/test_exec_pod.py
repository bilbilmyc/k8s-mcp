"""Tests for `exec_pod` — high-privilege batch-mode exec into a pod.

We monkeypatch `_core_v1` to feed a fake API whose
`connect_get_namespaced_pod_exec` returns a controllable fake WSClient.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from kubernetes.client.rest import ApiException

from k8s_mcp.config import reset_settings_cache
from k8s_mcp.tools import pods


@pytest.fixture(autouse=True)
def _settings():
    """Default settings for write-enabled, unrestricted namespace."""
    import os
    os.environ["K8S_MCP_READ_ONLY"] = "false"
    os.environ.pop("K8S_MCP_NAMESPACE_ALLOWLIST", None)
    reset_settings_cache()
    yield
    reset_settings_cache()


# ---------- fake API / pod / WSClient ---------------------------------------


class _FakeContainer:
    def __init__(self, name: str):
        self.name = name


class _FakeSpec:
    def __init__(self, containers: list[_FakeContainer]):
        self.containers = containers


class _FakePod:
    def __init__(self, containers: list[_FakeContainer]):
        self.spec = _FakeSpec(containers)


class _FakeWSClient:
    """Stand-in for kubernetes.stream.ws_client.WSClient.

    Behavior controlled by the constructor kwargs so each test can
    script its own outcome (success / non-zero exit / timeout)."""

    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        exit_code: int = 0,
        is_open_after: bool = False,
    ):
        self._stdout = stdout
        self._stderr = stderr
        self._exit_code = exit_code
        self._is_open_after = is_open_after
        self.run_forever_called_with: float | None = None
        self.close_called = 0

    def run_forever(self, timeout=None):
        self.run_forever_called_with = timeout

    def is_open(self):
        return self._is_open_after

    def close(self):
        self.close_called += 1

    def read_channel(self, channel):
        if channel == 1:
            return self._stdout
        if channel == 2:
            return self._stderr
        return ""

    @property
    def returncode(self):
        return self._exit_code


class _FakeApi:
    def __init__(
        self,
        pod: _FakePod | None = None,
        *,
        ws_client: _FakeWSClient | None = None,
        read_raises: Exception | None = None,
        exec_raises: Exception | None = None,
    ):
        self._pod = pod
        self._ws_client = ws_client
        self._read_raises = read_raises
        self._exec_raises = exec_raises
        self.exec_kwargs: dict = {}

    def read_namespaced_pod(self, name, namespace, **kwargs):
        if self._read_raises is not None:
            raise self._read_raises
        return self._pod

    def connect_get_namespaced_pod_exec(self, name, namespace, **kwargs):
        self.exec_kwargs = {"name": name, "namespace": namespace, **kwargs}
        if self._exec_raises is not None:
            raise self._exec_raises
        return self._ws_client


# ---------- validation ------------------------------------------------------


def test_rejects_empty_pod_name():
    with pytest.raises(ValueError, match="pod_name"):
        pods.exec_pod("", ["ls"])


def test_rejects_non_string_pod_name():
    with pytest.raises(ValueError, match="pod_name"):
        pods.exec_pod(123, ["ls"])  # type: ignore[arg-type]


def test_rejects_empty_command_list():
    with pytest.raises(ValueError, match="command"):
        pods.exec_pod("p", [])


def test_rejects_non_list_command():
    with pytest.raises(ValueError, match="command"):
        pods.exec_pod("p", "ls -la")  # type: ignore[arg-type]


def test_rejects_command_with_empty_string_arg():
    with pytest.raises(ValueError, match="non-empty strings"):
        pods.exec_pod("p", ["ls", ""])


def test_rejects_command_with_non_string_arg():
    with pytest.raises(ValueError, match="non-empty strings"):
        pods.exec_pod("p", ["ls", 123])  # type: ignore[arg-type]


def test_rejects_timeout_below_minimum():
    with pytest.raises(ValueError, match="timeout_seconds"):
        pods.exec_pod("p", ["ls"], timeout_seconds=0)


def test_rejects_timeout_above_maximum():
    with pytest.raises(ValueError, match="timeout_seconds"):
        pods.exec_pod("p", ["ls"], timeout_seconds=601)


# ---------- safety gates ----------------------------------------------------


def test_rejects_in_read_only_mode():
    import os
    os.environ["K8S_MCP_READ_ONLY"] = "true"
    reset_settings_cache()
    with pytest.raises(PermissionError, match="read-only"):
        pods.exec_pod("p", ["ls"])
    os.environ["K8S_MCP_READ_ONLY"] = "false"
    reset_settings_cache()


def test_rejects_when_namespace_not_in_allowlist():
    import os
    os.environ["K8S_MCP_NAMESPACE_ALLOWLIST"] = "allowed"
    reset_settings_cache()
    with pytest.raises(PermissionError, match="not allowed"):
        pods.exec_pod("p", ["ls"], namespace="other")
    os.environ.pop("K8S_MCP_NAMESPACE_ALLOWLIST", None)
    reset_settings_cache()


# ---------- happy path ------------------------------------------------------


def test_happy_path_renders_stdout_and_exit_code():
    pod = _FakePod([_FakeContainer("app")])
    ws = _FakeWSClient(stdout="hello world\n", stderr="", exit_code=0)
    api = _FakeApi(pod=pod, ws_client=ws)

    with patch.object(pods, "_core_v1", return_value=api):
        out = pods.exec_pod("web-1", ["echo", "hello"], namespace="app")

    assert "$ echo hello" in out
    assert "hello world" in out
    assert "(exit code: 0)" in out


def test_non_zero_exit_code_surfaces_in_output():
    pod = _FakePod([_FakeContainer("app")])
    ws = _FakeWSClient(stdout="", stderr="oh no\n", exit_code=2)
    api = _FakeApi(pod=pod, ws_client=ws)

    with patch.object(pods, "_core_v1", return_value=api):
        out = pods.exec_pod("p1", ["false"], namespace="app")

    assert "(exit code: 2)" in out
    assert "oh no" in out


def test_empty_stdout_and_stderr_omitted():
    pod = _FakePod([_FakeContainer("app")])
    ws = _FakeWSClient(stdout="", stderr="", exit_code=0)
    api = _FakeApi(pod=pod, ws_client=ws)

    with patch.object(pods, "_core_v1", return_value=api):
        out = pods.exec_pod("p1", ["true"], namespace="app")

    # Only the command line + exit code, nothing else.
    assert out == "$ true\n(exit code: 0)"


# ---------- pod / container handling ----------------------------------------


def test_auto_picks_container_in_single_container_pod():
    pod = _FakePod([_FakeContainer("only")])
    ws = _FakeWSClient(stdout="ok", exit_code=0)
    api = _FakeApi(pod=pod, ws_client=ws)

    with patch.object(pods, "_core_v1", return_value=api):
        pods.exec_pod("p1", ["ls"], namespace="app")

    assert api.exec_kwargs["container"] == "only"
    assert api.exec_kwargs["name"] == "p1"
    assert api.exec_kwargs["namespace"] == "app"


def test_explicit_container_passed_through():
    pod = _FakePod([_FakeContainer("main"), _FakeContainer("sidecar")])
    ws = _FakeWSClient(stdout="ok", exit_code=0)
    api = _FakeApi(pod=pod, ws_client=ws)

    with patch.object(pods, "_core_v1", return_value=api):
        pods.exec_pod(
            "p1", ["ls"], namespace="app", container="sidecar",
        )

    assert api.exec_kwargs["container"] == "sidecar"


def test_multi_container_pod_without_container_arg_raises_with_options():
    pod = _FakePod([
        _FakeContainer("main"),
        _FakeContainer("sidecar"),
    ])
    ws = _FakeWSClient(stdout="ok", exit_code=0)
    api = _FakeApi(pod=pod, ws_client=ws)

    with patch.object(pods, "_core_v1", return_value=api):
        with pytest.raises(ValueError) as excinfo:
            pods.exec_pod("p1", ["ls"], namespace="app")
    msg = str(excinfo.value)
    assert "multiple containers" in msg
    assert "main" in msg
    assert "sidecar" in msg


def test_unknown_container_raises_with_available_list():
    pod = _FakePod([_FakeContainer("main")])
    ws = _FakeWSClient(stdout="ok", exit_code=0)
    api = _FakeApi(pod=pod, ws_client=ws)

    with patch.object(pods, "_core_v1", return_value=api):
        with pytest.raises(ValueError) as excinfo:
            pods.exec_pod("p1", ["ls"], namespace="app", container="nope")
    msg = str(excinfo.value)
    assert "'nope'" in msg
    assert "['main']" in msg or "'main'" in msg


def test_pod_not_found_raises_value_error():
    api = _FakeApi(read_raises=ApiException(status=404, reason="not found"))

    with patch.object(pods, "_core_v1", return_value=api):
        with pytest.raises(ValueError, match="not found"):
            pods.exec_pod("ghost", ["ls"], namespace="app")


def test_pod_read_other_error_raises_runtime():
    api = _FakeApi(read_raises=ApiException(status=403, reason="forbidden"))

    with patch.object(pods, "_core_v1", return_value=api):
        with pytest.raises(RuntimeError, match="403"):
            pods.exec_pod("p1", ["ls"], namespace="app")


# ---------- exec WebSocket handling -----------------------------------------


def test_exec_kwargs_include_required_fields():
    pod = _FakePod([_FakeContainer("app")])
    ws = _FakeWSClient(stdout="", exit_code=0)
    api = _FakeApi(pod=pod, ws_client=ws)

    with patch.object(pods, "_core_v1", return_value=api):
        pods.exec_pod("p1", ["ls", "-la"], namespace="app")

    kw = api.exec_kwargs
    assert kw["command"] == ["ls", "-la"]
    assert kw["stdout"] is True
    assert kw["stderr"] is True
    assert kw["stdin"] is False
    assert kw["tty"] is False
    # _preload_content=False is what gives us back a WSClient instead
    # of a string — load-bearing for the batch-mode implementation.
    assert kw["_preload_content"] is False


def test_exec_api_error_raises_runtime_with_resource_locator():
    pod = _FakePod([_FakeContainer("app")])
    api = _FakeApi(
        pod=pod,
        exec_raises=ApiException(status=403, reason="forbidden"),
    )

    with patch.object(pods, "_core_v1", return_value=api):
        with pytest.raises(RuntimeError) as excinfo:
            pods.exec_pod("p1", ["ls"], namespace="app")
    msg = str(excinfo.value)
    assert "default/p1/app" in msg or "app/p1" in msg
    assert "403" in msg


def test_timeout_closes_ws_and_returns_friendly_message():
    pod = _FakePod([_FakeContainer("app")])
    # is_open_after=True simulates run_forever returning but WS still
    # alive — that's our wall-clock timeout signal.
    ws = _FakeWSClient(is_open_after=True)
    api = _FakeApi(pod=pod, ws_client=ws)

    with patch.object(pods, "_core_v1", return_value=api):
        out = pods.exec_pod(
            "p1", ["sleep", "60"], namespace="app", timeout_seconds=5,
        )

    assert "exec timeout after 5s" in out
    assert "pod may still be running" in out
    assert ws.run_forever_called_with == 5
    assert ws.close_called == 1  # we closed the dangling WS


def test_websocket_exception_wraps_in_runtime():
    pod = _FakePod([_FakeContainer("app")])

    class _BrokenWS(_FakeWSClient):
        def run_forever(self, timeout=None):
            raise RuntimeError("connection reset")

    api = _FakeApi(pod=pod, ws_client=_BrokenWS())

    with patch.object(pods, "_core_v1", return_value=api):
        with pytest.raises(RuntimeError, match="WebSocket error"):
            pods.exec_pod("p1", ["ls"], namespace="app")
