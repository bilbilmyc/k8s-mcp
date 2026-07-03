"""All tools must register with structured_output=False (outputSchema=None).

Why: FastMCP 1.28.1 wraps tools with typed returns in `{"result": ...}` and
emits an outputSchema. Cherry Studio then JSON-encodes that envelope into
content[0].text, forcing agent code to unwrap two layers. _K8sMCP overrides
add_tool to default structured_output=None → False so the raw tool return
goes straight into content[0].text — what spec-compliant clients show
anyway. This test locks that behavior.
"""
from __future__ import annotations

import pytest

from k8s_mcp.server import _K8sMCP, create_server


@pytest.mark.asyncio
async def test_all_tools_have_no_output_schema():
    """Every registered tool must have outputSchema=None — the wrap that
    breaks Cherry Studio parsing is gated on outputSchema being set."""
    mcp = _K8sMCP("probe")
    # Use the same registration the real server runs.
    from k8s_mcp import server as srv_mod
    srv_mod._register_tools(mcp)

    tools = await mcp.list_tools()
    assert tools, "expected at least one tool registered"
    bad = [t.name for t in tools if t.outputSchema is not None]
    assert not bad, (
        f"Tools with outputSchema set will get the `{{result: ...}}` wrap "
        f"that breaks Cherry Studio parsing: {bad}"
    )


@pytest.mark.asyncio
async def test_create_server_uses_k8s_subclass():
    """create_server() must return the _K8sMCP subclass, not raw FastMCP."""
    mcp = create_server()
    assert isinstance(mcp, _K8sMCP)


def test_structured_output_true_still_works():
    """Explicit structured_output=True must still emit a schema — opt-in
    path stays open for any future tool that wants structured content."""
    mcp = _K8sMCP("probe")

    @mcp.tool(structured_output=True)
    def wants_structured() -> str:
        """A tool that wants the wrap."""
        return "structured"

    import asyncio

    async def go():
        tools = await mcp.list_tools()
        t = next(t for t in tools if t.name == "wants_structured")
        assert t.outputSchema is not None
        # Sanity: default tools (no kwarg) get None
        @mcp.tool()
        def wants_unstructured() -> str:
            return "unstructured"
        tools2 = await mcp.list_tools()
        t2 = next(t for t in tools2 if t.name == "wants_unstructured")
        assert t2.outputSchema is None

    asyncio.run(go())
