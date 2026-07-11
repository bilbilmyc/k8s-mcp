"""k8s-mcp server entry point.

中文说明：
这是 k8s-mcp 的 FastMCP 入口，负责：

  1. 加载 Settings（K8S_MCP_* 环境变量）
  2. 注册所有 tools/*.py 下的 `register(mcp)` 函数
  3. 通过 stdio 与 LLM Agent（Claude Desktop / Cursor / Cherry Studio 等）通信

整个 server 是单进程的，所有 tool 调用串行执行；状态都保存在
进程内（client 缓存、OpenAPI schema 缓存）。

Operational safety nets applied at the `call_tool` boundary (see
`k8s_mcp.safety` for details): per-tool rate limit, per-call wall-clock
timeout, and apiserver-error sanitization. Each is opt-out via
`K8S_MCP_RATE_LIMIT_RPM=0` / `K8S_MCP_TOOL_TIMEOUT_S=0`.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Sequence
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ContentBlock

from . import __version__
from .config import Settings, get_settings
from .safety import (
    RateLimiter,
    SafeApiError,
    ToolTimeoutError,
    safe_apiserver_error,
)

logger = logging.getLogger(__name__)


class _K8sMCP(FastMCP):
    """FastMCP subclass that defaults structured_output=None → False AND
    enforces three production safety nets on every `call_tool`:

      1. **Per-tool rate limit** — `K8S_MCP_RATE_LIMIT_RPM` (default 120)
         caps how often a single tool can be called per minute. A
         runaway agent that hammers `list_pods` cannot saturate the
         apiserver or the MCP transport. The cap is per-tool-name, not
         global, so a busy agent can still mix calls (list + describe +
         logs in parallel) without one blocking the other.
      2. **Per-call wall-clock timeout** — `K8S_MCP_TOOL_TIMEOUT_S`
         (default 60s) bounds how long any single tool call can run.
         Implemented by offloading the sync tool body to the default
         executor and `asyncio.wait_for`-ing the future. The orphan
         task is *not* cancelled (Python has no portable way to kill a
         sync thread); it will complete or hit the apiserver's own
         timeout. The MCP request returns immediately.
      3. **Apiserver error sanitization** — every `ApiException` from
         the kubernetes client is mapped to a curated `SafeApiError`
         whose message is a one-liner. The raw `body` (which can
         include RBAC details, internal hostnames, manifest field
         paths) is never exposed to the LLM.

    Each safety net can be disabled independently: `RATE_LIMIT_RPM=0`
    turns the limiter off; `TOOL_TIMEOUT_S=0` skips the timeout wrap;
    the error sanitizer is always on (turning it off would defeat the
    point of having it).

    `structured_output` defaults to `False` for the same reason as
    before: FastMCP 1.28.1 wraps `-> str` returns in a `{"result": ...}`
    envelope and Cherry Studio JSON-encodes that envelope into
    `content[0].text`, forcing the agent to unwrap a second layer. With
    `structured_output=False` the LLM sees the raw table string.
    """

    def __init__(self, *args, settings: Settings | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        s = settings or get_settings()
        # RateLimiter is a no-op when rpm=0; check() is the gate.
        self._rate_limiter = RateLimiter(rpm=s.rate_limit_rpm)
        self._tool_timeout_s = float(s.tool_timeout_s)
        logger.info(
            "safety nets: rate_limit_rpm=%d tool_timeout_s=%s",
            s.rate_limit_rpm,
            f"{self._tool_timeout_s:g}s" if self._tool_timeout_s > 0 else "off",
        )

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

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> Sequence[ContentBlock] | dict[str, Any]:
        # 1. Rate limit — fail fast BEFORE entering the apiserver call.
        if self._rate_limiter._rpm > 0:
            self._rate_limiter.check(name)

        # 2. Per-call timeout + 3. apiserver-error sanitization.
        # The registered tool bodies are all sync (kubernetes client
        # calls are blocking). `asyncio.wait_for(super().call_tool(...))`
        # does NOT work for timeout enforcement: the sync body runs
        # inline inside FastMCP's async `tool.run()`, and wait_for has
        # no yield point to cancel at — the sleep / apiserver call
        # runs to completion no matter what the timeout is set to.
        #
        # Workaround: bypass FastMCP's async wrapper and dispatch the
        # sync tool body on the default executor. Then `wait_for` on
        # the returned future will actually fire at the timeout. The
        # orphan executor task isn't cancelled (Python can't kill a
        # sync thread), but the MCP request returns immediately with a
        # `ToolTimeoutError` and the LLM can move on; the orphan will
        # finish (or hit the apiserver's own timeout) on its own.
        tool = self._tool_manager._tools.get(name)
        if tool is None:
            # Unknown tool — let FastMCP raise its standard ToolError
            # so the error path is identical to the pre-hardening
            # behavior for typos / unknown names.
            return await super().call_tool(name, arguments)

        # Capture the context on the main loop (it touches the running
        # session), then pass it into the executor thread. `Context` is
        # not safe to construct from a worker thread.
        ctx = self.get_context() if tool.context_kwarg else None
        meta = tool.fn_metadata
        loop = asyncio.get_event_loop()

        def _invoke() -> Any:
            # Replicate what `Tool.run` does for a sync tool, but
            # without the `async` wrapper — so this function blocks
            # the executor thread (and `asyncio.wait_for` on the main
            # loop can actually cancel the future at the timeout).
            #
            # We deliberately skip `meta.convert_result` here: that
            # helper returns a `(unstructured, structured)` tuple for
            # `-> str` tools, which the call_tool / lowlevel boundary
            # then re-normalizes. Returning the raw string / dict /
            # etc. and letting the lowlevel layer wrap it matches
            # FastMCP's pre-hardening output exactly.
            pre_parsed = meta.pre_parse_json(arguments)
            parsed_model = meta.arg_model.model_validate(pre_parsed)
            kwargs = parsed_model.model_dump_one_level()
            if ctx is not None and tool.context_kwarg:
                kwargs[tool.context_kwarg] = ctx
            return tool.fn(**kwargs)

        try:
            if self._tool_timeout_s > 0:
                raw = await asyncio.wait_for(
                    loop.run_in_executor(None, _invoke),
                    timeout=self._tool_timeout_s,
                )
            else:
                raw = await loop.run_in_executor(None, _invoke)
        except TimeoutError as e:
            logger.warning("tool %r hit %ss timeout", name, self._tool_timeout_s)
            raise ToolTimeoutError(name, self._tool_timeout_s) from e
        except SafeApiError:
            raise
        except PermissionError:
            # App-layer permission denials (read-only mode / namespace
            # allowlist). The message names the violated setting so the
            # agent can fix the call.
            raise
        except Exception as e:
            # FastMCP's `Tool.run` wraps every tool-body exception in
            # `ToolError(f"Error executing tool {name}: {e}")` from e.
            # We DO go through `Tool.run` (via the executor path), so
            # the raw `ApiException` / `ValueError` is at
            # `e.__cause__`, not `e` itself. Recover it for
            # sanitization; the operator still sees the full chain
            # in the log warning.
            from kubernetes.client.rest import ApiException
            cause = e.__cause__ or e.__context__ or e
            if isinstance(cause, ApiException):
                safe = safe_apiserver_error(cause, op="call", kind=name)
                logger.info(
                    "tool %r: apiserver HTTP %s sanitized to %r",
                    name, cause.status, str(safe),
                )
                raise safe from e
            logger.warning(
                "tool %r failed: %s (cause=%s): %s",
                name, type(cause).__name__, type(e).__name__, cause,
            )
            raise SafeApiError(
                status=0,
                reason=type(cause).__name__,
                message=f"call {name} failed: internal {type(cause).__name__}",
                hint="this is an internal error; check the server logs for details",
            ) from e

        # Tool body succeeded. Wrap the raw return into the
        # ContentBlock list the call_tool contract expects. We do this
        # here (not inside `_invoke`) so the executor thread stays
        # strictly synchronous — that way `asyncio.wait_for` on the
        # main loop can actually fire at the timeout.
        return meta.convert_result(raw)


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
    logger.info(
        "k8s-mcp %s starting (read_only=%s)", __version__, settings.read_only
    )

    _install_signal_handlers()

    mcp = _K8sMCP("k8s-mcp", settings=settings)

    @mcp.tool()
    def ping() -> str:
        """Health check. Returns the k8s-mcp version (e.g. `pong (0.2.2)`)."""
        return f"pong (k8s-mcp {__version__})"

    _register_tools(mcp)
    return mcp


# ---------- graceful shutdown -------------------------------------------------


_in_flight = 0


def _install_signal_handlers() -> None:
    """Install SIGTERM/SIGINT handlers that log in-flight tool count.

    FastMCP already handles clean shutdown of the stdio loop; this is purely
    a diagnostic — operators want to see "still busy with N tool calls" in
    the logs when they Ctrl-C a long-running health snapshot or Prometheus
    range query. We don't block the signal (FastMCP needs to be able to
    exit promptly).
    """

    def _handler(signum, _frame):  # noqa: ANN001 — signal handler signature
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = str(signum)
        logger.warning(
            "received %s — %d tool call(s) still in flight", name, _in_flight
        )

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # SIGTERM may not be installable on Windows + non-main threads.
            pass


def in_flight_inc() -> None:
    global _in_flight
    _in_flight += 1


def in_flight_dec() -> None:
    global _in_flight
    if _in_flight > 0:
        _in_flight -= 1


def _register_tools(mcp: FastMCP) -> None:
    """注册 MCP 工具。每个模块使用 `register(mcp)` 模式挂载。

    中文说明：
    所有 tools/*.py 都遵循 `def register(mcp)` 约定；这里集中调用，
    后续新增 tool 模块只要在两个地方 import + 调用即可。
    """
    from .tools import (
        autoscale,
        certs,
        cluster_info,
        configmap,
        delete_tool,
        diagnostics,
        discovery,
        events,
        explain,
        generic,
        health,
        logs,
        metrics,
        namespace,
        networkpolicy,
        node_ops,
        notifier,
        pods,
        prometheus,
        rbac,
        resource_usage,
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
    diagnostics.register(mcp)
    explain.register(mcp)
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
    resource_usage.register(mcp)
    serviceaccount.register(mcp)
    storage.register(mcp)
    prometheus.register(mcp)
    certs.register(mcp)
    health.register(mcp)
    notifier.register(mcp)
    namespace.register(mcp)
    cluster_info.register(mcp)


def main() -> None:
    """脚本入口：构建 server 并以 stdio 方式跑起来。"""
    mcp = create_server()
    mcp.run()


if __name__ == "__main__":
    main()
