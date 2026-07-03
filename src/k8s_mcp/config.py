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
    delete_token_secret: str = "change-me"
    delete_token_ttl_seconds: int = 300

    # Prometheus（可选，监控查询）
    # 显式 URL 优先；未设置则按候选 (namespace, service) 自动探测。
    prometheus_url: str | None = None
    prometheus_bearer_token: str | None = None

    # 引导性集群组件：local-path-provisioner 之类单 manifest 部署
    # 集群基础设施。默认指向公开 manifest，私有/离线集群可以通过
    # 环境变量指向自家镜像。
    local_path_provisioner_url: str = (
        "https://raw.githubusercontent.com/rancher/local-path-provisioner/master/deploy/local-path-storage.yaml"
    )

    # 通知 webhook 列表。JSON 字符串，每项是
    # `{"name": "<id>", "type": "feishu|slack|wecom|generic",
    #   "url": "https://...", "cluster_label": "<optional>"}`。
    # 例：`K8S_MCP_NOTIFIERS='[{"name":"ops","type":"feishu",
    #   "url":"https://open.feishu.cn/...","cluster_label":"prod"}]'`
    notifiers: str | None = None

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
