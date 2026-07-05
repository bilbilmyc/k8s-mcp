"""Fixtures for integration tests.

Provides:
  - `cluster`: skips the whole module if no reachable cluster.
  - `clean_namespace`: creates a throwaway namespace per test, deletes it after.
  - `tool_call`: invokes a registered tool function by name with kwargs.
"""
from __future__ import annotations

import uuid

import pytest

from k8s_mcp import server


def _cluster_reachable() -> bool:
    """Cheap connectivity probe. Avoids hanging tests when no cluster exists.

    Returns False for ANY error (no auth, no network, apiserver down).
    The `pytest.skipif` mark uses this to skip the entire module cleanly
    instead of letting the first test fail with an unintelligible error.
    """
    try:
        from kubernetes import client

        from k8s_mcp.client import get_api_client

        api = client.VersionApi(get_api_client())
        v = api.get_code()
        return bool(v and v.git_version)
    except Exception:  # noqa: BLE001 — probe is best-effort, ANY error means "no cluster"
        return False


pytestmark = pytest.mark.skipif(
    not _cluster_reachable(),
    reason="No reachable Kubernetes cluster — set KUBECONFIG or K8S_MCP_API_SERVER",
)


@pytest.fixture(scope="session")
def mcp_server():
    """Build the FastMCP server once per session and return it.

    Tests register their own tools by importing modules; the server itself
    is wired up by `create_server`. We use it to introspect the tool
    registry via `tool_call`.
    """
    return server.create_server()


@pytest.fixture
def clean_namespace(mcp_server):
    """Create a unique namespace, yield its name, delete it after the test."""

    from kubernetes import client
    from kubernetes.client.rest import ApiException

    from k8s_mcp.client import get_api_client

    ns_name = f"k8s-mcp-it-{uuid.uuid4().hex[:8]}"
    core = client.CoreV1Api(get_api_client())
    body = client.V1Namespace(metadata=client.V1ObjectMeta(name=ns_name))
    core.create_namespace(body=body)

    try:
        yield ns_name
    finally:
        try:
            core.delete_namespace(ns_name, body=client.V1DeleteOptions())
        except ApiException:
            pass  # already gone


@pytest.fixture
def tool_call(mcp_server):
    """Return a callable that invokes a registered tool by name.

    Usage:
        def test_x(tool_call):
            out = tool_call("list_resources", kind="Pod", namespace="default")
            assert "NAME" in out
    """

    async def _call(name: str, **kwargs):
        tools = await mcp_server.list_tools()
        match = next((t for t in tools if t.name == name), None)
        if match is None:
            raise AssertionError(f"tool {name!r} not registered")
        # FastMCP's tool runner accepts kwargs; we just delegate to the
        # underlying function via the tool's metadata.
        # In practice, integration tests should import the tool function
        # directly to avoid dealing with the MCP transport layer. This
        # fixture is here for completeness — see test_apply_delete.py.
        raise NotImplementedError(
            "Use the tool function directly: e.g. `generic.list_resources(...)`. "
            "This fixture is reserved for future end-to-end MCP transport tests."
        )

    return _call
