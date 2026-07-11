"""Namespace creation shortcut.

中文说明：
`create_namespace` 是对通用 `apply_yaml` 的速记——把 `{apiVersion, kind,
metadata, spec}` 拼装交给工具函数处理，避免 Agent 手写 YAML 时漏字段
（最常踩坑的是 `metadata.labels` 与 `spec.finalizers`）。

Namespace 是 cluster-scoped 资源；守门与 Node/ClusterRole 一致：
`READ_ONLY=true` 一律拒；`NAMESPACE_ALLOWLIST` 设了就拒（cluster-scoped
写入一律拒绝，避免误改集群级对象）。
"""
from __future__ import annotations

import logging
import re

import yaml

from ..config import get_settings
from . import generic

logger = logging.getLogger(__name__)


_NS_NAME_RE = re.compile(
    r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$"
)

# K8s finalizers that, if present, prevent immediate deletion. We don't
# auto-attach `kubernetes` (the default) — that's only meaningful when
# the apiserver is configured for it; leaving it empty matches
# `kubectl create namespace <name>` default behavior.
_ALLOWED_LABELS_KEY_RE = re.compile(
    r"^([a-zA-Z0-9]([-a-zA-Z0-9_.]{0,61}[a-zA-Z0-9])?/?)*$"
)


def _validate_ns_name(name: str) -> None:
    if not name or not _NS_NAME_RE.match(name):
        raise ValueError(
            f"invalid namespace name: {name!r}; "
            "must match RFC 1123 label (lowercase alphanumeric, dashes)"
        )


def _validate_labels(labels: dict[str, str] | None) -> None:
    if labels is None:
        return
    for k, v in labels.items():
        if not k or not _ALLOWED_LABELS_KEY_RE.match(k):
            raise ValueError(f"invalid label key: {k!r}")
        if v is None or not _ALLOWED_LABELS_KEY_RE.match(v):
            raise ValueError(f"invalid label value for {k!r}: {v!r}")


def _read_only_guard(action: str) -> None:
    settings = get_settings()
    if settings.read_only:
        raise PermissionError(
            f"Server is in read-only mode (K8S_MCP_READ_ONLY=true). "
            f"{action} is disabled."
        )
    if settings.namespace_allowlist is not None:
        raise PermissionError(
            "Namespace is a cluster-scoped resource; cluster-scoped writes "
            "are refused when K8S_MCP_NAMESPACE_ALLOWLIST is set. Use an "
            "unset allowlist for cluster-scoped writes."
        )


def create_namespace(
    name: str,
    labels: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
) -> str:
    """⚠️ WRITE / ⚠️ CLUSTER-SCOPED — create a Namespace — pick THIS when you
    need a fresh tenant / environment (per-stage, per-team, per-customer
    isolation) and don't want to hand-write the YAML manifest.

    Equivalent to `kubectl create namespace <name> [--labels=...]` but
    validates inputs client-side and delegates the actual apply to
    `apply_yaml` so the same safety nets (read_only guard + structured
    result) apply.

    Args:
        name: namespace name (RFC 1123 label — lowercase alphanumeric,
            dashes; max 63 chars).
        labels: optional dict of labels to attach (e.g.
            `{"env": "prod", "team": "platform"}`).
        annotations: optional dict of annotations (e.g.
            `{"contact": "platform@example.com"}`).

    Returns the apply result (kind/name: action).

    Raises:
        ValueError: invalid name / labels.
        PermissionError: read-only mode or namespace_allowlist is set.
    """
    _read_only_guard("create_namespace")
    _validate_ns_name(name)
    _validate_labels(labels)

    md: dict = {"name": name}
    if labels:
        md["labels"] = labels
    if annotations:
        md["annotations"] = annotations

    manifest = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": md,
        "spec": {},
    }
    return generic.apply_yaml(yaml.safe_dump(manifest))


def register(mcp) -> None:
    mcp.tool()(create_namespace)
