"""k8s-mcp server entry point.

中文说明：
这是 k8s-mcp 的 FastMCP 入口，负责：

  1. 加载 Settings（K8S_MCP_* 环境变量）
  2. 注册所有 tools/*.py 下的 `register(mcp)` 函数
  3. 通过 stdio 与 LLM Agent（Claude Desktop / Cursor / Cherry Studio 等）通信

整个 server 是单进程的，所有 tool 调用串行执行；状态都保存在
进程内（client 缓存、OpenAPI schema 缓存）。
"""
from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import Settings, get_settings

logger = logging.getLogger(__name__)


class _K8sMCP(FastMCP):
    """FastMCP subclass that defaults structured_output=None → False.

    FastMCP 1.28.1 wraps tools that have a typed return (incl. `-> str`) in
    a `{"result": ...}` envelope and emits an outputSchema. Cherry Studio
    then JSON-encodes that envelope into `content[0].text`, forcing agent
    code to unwrap a second layer just to read the table string. With
    structured_output=False, outputSchema is None and `content[0].text`
    is the raw string — what Claude Desktop / spec-compliant clients
    show anyway.

    Tools that genuinely want structured content can opt in explicitly
    with `@mcp.tool(structured_output=True)`.
    """

    def add_tool(
        self,
        fn,
        name=None,
        title=None,
        description=None,
        annotations=None,
        icons=None,
        meta=None,
        structured_output: bool | None = None,
        **kwargs: Any,
    ) -> None:
        if structured_output is None:
            structured_output = False
        super().add_tool(
            fn,
            name=name,
            title=title,
            description=description,
            annotations=annotations,
            icons=icons,
            meta=meta,
            structured_output=structured_output,
            **kwargs,
        )


def create_server(settings: Settings | None = None) -> FastMCP:
    """构建并返回 FastMCP server，所有 tool 已注册。

    中文说明：
    这是 k8s-mcp 的入口；通过 `_register_tools` 把所有 tools/*.py 下的
    register(mcp) 串起来，统一挂载到 FastMCP 实例上。
    """
    settings = settings or get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info("k8s-mcp starting (read_only=%s)", settings.read_only)

    mcp = _K8sMCP("k8s-mcp")

    @mcp.tool()
    def ping() -> str:
        """Health check. Returns 'pong'."""
        return "pong"

    _register_tools(mcp)
    return mcp


def _register_tools(mcp: FastMCP) -> None:
    """注册 MCP 工具。每个模块使用 `register(mcp)` 模式挂载。

    中文说明：
    所有 tools/*.py 都遵循 `def register(mcp)` 约定；这里集中调用，
    后续新增 tool 模块只要在两个地方 import + 调用即可。
    """
    from .tools import (
        autoscale,
        bulk,
        certs,
        cluster_info,
        configmap,
        delete_tool,
        discovery,
        events,
        generic,
        health,
        logs,
        metrics,
        networkpolicy,
        node_ops,
        notifier,
        pods,
        prometheus,
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
    prometheus.register(mcp)
    certs.register(mcp)
    health.register(mcp)
    bulk.register(mcp)
    notifier.register(mcp)
    cluster_info.register(mcp)


def main() -> None:
    """脚本入口：构建 server 并以 stdio 方式跑起来。"""
    mcp = create_server()
    mcp.run()


if __name__ == "__main__":
    main()
