"""k8s-mcp server entry point."""
from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from .config import Settings, get_settings

logger = logging.getLogger(__name__)


def create_server(settings: Settings | None = None) -> FastMCP:
    settings = settings or get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info("k8s-mcp starting (read_only=%s)", settings.read_only)

    mcp = FastMCP("k8s-mcp")

    @mcp.tool()
    def ping() -> str:
        """Health check. Returns 'pong'."""
        return "pong"

    _register_tools(mcp)
    return mcp


def _register_tools(mcp: FastMCP) -> None:
    """Register MCP tools. Each module uses the @mcp.tool decorator pattern."""
    from .tools import (
        autoscale,
        configmap,
        delete_tool,
        discovery,
        events,
        generic,
        logs,
        metrics,
        networkpolicy,
        node_ops,
        pods,
        rbac,
        rollout,
        secret,
        service,
        serviceaccount,
        storage,
        wait_tool,
        workload,
    )
    from .tools import (
        jsonpath as jsonpath_tools,
    )

    generic.register(mcp)
    logs.register(mcp)
    events.register(mcp)
    pods.register(mcp)
    workload.register(mcp)
    service.register(mcp)
    configmap.register(mcp)
    delete_tool.register(mcp)
    metrics.register(mcp)
    rollout.register(mcp)
    node_ops.register(mcp)
    wait_tool.register(mcp)
    jsonpath_tools.register(mcp)
    secret.register(mcp)
    discovery.register(mcp)
    autoscale.register(mcp)
    rbac.register(mcp)
    networkpolicy.register(mcp)
    serviceaccount.register(mcp)
    storage.register(mcp)


def main() -> None:
    mcp = create_server()
    mcp.run()


if __name__ == "__main__":
    main()
