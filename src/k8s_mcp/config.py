"""Runtime settings for k8s-mcp.

All environment variables are prefixed with K8S_MCP_.

中文说明：
本模块集中管理 k8s-mcp 的所有运行时配置。所有环境变量都以 `K8S_MCP_`
为前缀（例如 `K8S_MCP_READ_ONLY` 对应 `read_only`）。配置被划分为三大类：

  - 日志/输出（log_level、default_tail_lines）
  - 三档认证：apiserver+token / kubeconfig / in-cluster
  - 安全守门：read-only 模式、namespace allowlist、删除二次确认的 HMAC 密钥与 TTL
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Pydantic-Settings 模型，承载全部 K8S_MCP_* 环境变量。

    中文说明：
    配置项详见 README 中的 "Safety flags" 与 "Authentication" 章节；
    ns_allowed() 是写工具（write tool）调用的统一守门入口，
    既负责 read_only 检查，也负责 namespace allowlist 检查。
    """

    model_config = SettingsConfigDict(
        env_prefix="K8S_MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 日志与输出 / Logging / output
    log_level: str = "INFO"
    default_tail_lines: int = 100

    # 认证模式 A：apiserver URL + token / Auth mode A: apiserver URL + token
    api_server: str | None = None
    api_token: str | None = None
    api_ca_cert: str | None = None
    api_insecure: bool = False

    # 认证模式 B：kubeconfig / Auth mode B: kubeconfig
    kubeconfig: str | None = None
    kube_context: str | None = None

    # 安全守门 / Safety
    read_only: bool = False
    namespace_allowlist: list[str] | None = None

    # Operational safety nets (P0 hardening for production). Applied at
    # the FastMCP call_tool boundary in server.py:
    #   - rate_limit_rpm: per-tool requests-per-minute cap. 0 = disabled.
    #     120 RPM (~2 RPS) is the default — generous for an interactive
    #     agent, restrictive enough to keep one runaway loop from
    #     saturating the apiserver.
    #   - tool_timeout_s: wall-clock cap on a single tool call. 60s is
    #     enough for list/describe/apply; raise it if you depend on
    #     long-running `rollout_status(watch=True)` or Prometheus range
    #     queries that legitimately need minutes.
    rate_limit_rpm: int = 120
    tool_timeout_s: float = 60.0

    # Prometheus（可选，监控查询）
    # 显式 URL 优先；未设置则按候选 (namespace, service) 自动探测。
    prometheus_url: str | None = None
    prometheus_bearer_token: str | None = None
    # 发现侧 namespace 白名单。None（默认）= 扫全集群；设置后，
    # `find_prometheus_service()` 与 `_resolve_prometheus_url()` 的宽
    # 扫描 fallback 都只扫这些 namespace。在多租户 / 大量 ns 的集群上
    # 用它限制扫面成本 / 信息暴露面。与 `namespace_allowlist`（写守门）
    # 是两套独立的配置：前者是**读**侧白名单。
    prometheus_namespace_allowlist: list[str] | None = None

    # 引导性集群组件：local-path-provisioner 之类单 manifest 部署
    # 集群基础设施。默认指向公开 manifest，私有/离线集群可以通过
    # 环境变量指向自家镜像。
    local_path_provisioner_url: str = (
        "https://raw.githubusercontent.com/rancher/local-path-provisioner/master/deploy/local-path-storage.yaml"
    )
    # metrics-server manifest URL for `bootstrap_metrics_server` (and the
    # auto-bootstrap path in top_pods/top_nodes). Default points at the
    # upstream kubernetes-sigs release; override for air-gapped installs.
    metrics_server_manifest_url: str | None = None

    # 通知 webhook 列表。JSON 字符串，每项是
    # `{"name": "<id>", "type": "feishu|slack|wecom|generic",
    #   "url": "https://...", "cluster_label": "<optional>"}`。
    # 例：`K8S_MCP_NOTIFIERS='[{"name":"ops","type":"feishu",
    #   "url":"https://open.feishu.cn/...","cluster_label":"prod"}]'`
    notifiers: str | None = None
    # notifier URL safety gate. By default only `https://` URLs are
    # accepted — refusing cleartext POSTs that would leak bearer tokens
    # / message contents in transit and refusing SSRF to internal hosts.
    # Set NOTIFIER_URL_ALLOW_HTTP=true to permit http for local testing.
    # Optional NOTIFIER_URL_ALLOWLIST is a comma-separated host allowlist
    # (exact match) — when set, https URLs whose host is not in the list
    # are refused too. Default behavior is "any https host".
    notifier_url_allow_http: bool = False
    notifier_url_allowlist: list[str] | None = None

    @field_validator("namespace_allowlist", mode="before")
    @classmethod
    def _split_allowlist(cls, v: Any) -> list[str] | None:
        """解析逗号分隔的环境变量值；空串视作 None。

        中文说明：
        允许通过 `K8S_MCP_NAMESPACE_ALLOWLIST=default,app,prod` 这种
        逗号分隔写法注入列表；空字符串等价于未设置（None = 不限制）。
        """
        if v is None or v == "":
            return None
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    @field_validator("prometheus_namespace_allowlist", mode="before")
    @classmethod
    def _split_prom_allowlist(cls, v: Any) -> list[str] | None:
        """与 `namespace_allowlist` 同形的逗号分隔解析（独立字段，独立的解析器）。"""
        if v is None or v == "":
            return None
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    @field_validator("notifier_url_allowlist", mode="before")
    @classmethod
    def _split_notifier_allowlist(cls, v: Any) -> list[str] | None:
        """与 `namespace_allowlist` 同形的逗号分隔解析（独立字段，独立的解析器）。"""
        if v is None or v == "":
            return None
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    def ns_allowed(self, namespace: str | None) -> bool:
        """返回该 namespace 是否允许写入。

        中文说明：
        所有写工具在执行前都应调用本方法。它一并处理 read_only 短路
        与 allowlist 匹配；当 allowlist 已设置但 namespace 为空
        （cluster-scoped 资源）时一律拒绝，避免误改集群级对象。
        """
        if self.read_only:
            return False
        if self.namespace_allowlist is None:
            return True
        if namespace is None:
            return False  # cluster-scoped writes not allowed when allowlist set
        return namespace in self.namespace_allowlist


@lru_cache
def get_settings() -> Settings:
    """获取单例 Settings；结果会被 lru_cache 缓存，测试里要用 reset 清掉。"""
    return Settings()


def reset_settings_cache() -> None:
    """清掉 Settings 单例缓存，方便测试改环境变量后重新加载。"""
    get_settings.cache_clear()
