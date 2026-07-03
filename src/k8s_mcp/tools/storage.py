"""Storage: create_pvc and the local-path-provisioner bootstrap.

`create_pvc` claims a single PersistentVolume. When the cluster has no
StorageClass at all, that PVC will sit Pending forever — most dev/test
clusters (kind, k3s default, minikube with no extra setup) hit this. The
escape hatch is `bootstrap_local_path_provisioner`: it applies Rancher's
local-path-storage manifest in one shot, giving the cluster a working
`local-path` StorageClass.

中文说明：
- `create_pvc`：单个 PVC 声明，集群必须已有对应 StorageClass 才能绑定。
- `bootstrap_local_path_provisioner`：在 SC 缺失时一次性 install 一个
  hostPath-based 的本地 provisioner（等价于
  `kubectl apply -f rancher/local-path-storage.yaml`）。装完之后
  `storage_class_name="local-path"` 立刻可用，PVC 提交即自动创建
  hostPath PV。生产环境慎用(hostPath 不抗节点故障),开发测试首选。
"""
from __future__ import annotations

import logging
import urllib.error
import urllib.request

from ..client import get_api_client  # noqa: F401  (used by tests indirectly)
from ..config import get_settings
from . import generic

logger = logging.getLogger(__name__)


# Default-annotation key Rancher uses. We grep to remove when the user
# asks NOT to mark this SC as the cluster default.
_DEFAULT_CLASS_ANNOTATION = (
    'storageclass.kubernetes.io/is-default-class: "true"'
)


# Module-level manifest cache. k8s-mcp's session lifetime is one MCP
# connection, so we don't bother invalidating; restart_clears_state
# covers cross-session reloads.
_manifest_cache: str | None = None


def _read_only_guard() -> None:
    if get_settings().read_only:
        raise PermissionError(
            "Server is in read-only mode (K8S_MCP_READ_ONLY=true). "
            "Bootstrap is disabled."
        )


def create_pvc(
    name: str,
    namespace: str,
    size: str,
    access_modes: list[str] | None = None,
    storage_class: str | None = None,
    labels: dict[str, str] | None = None,
) -> str:
    """⚠️ WRITE / ⚠️ PROVISIONS STORAGE — claims a PersistentVolume from the
    cluster. On cloud providers this is a billable resource (GB-month cost);
    ensure the size and storage_class are right before confirming with the
    user.

    Args:
        name: PVC name.
        namespace: target namespace.
        size: requested size, e.g. "1Gi", "10Gi".
        access_modes: defaults to ["ReadWriteOnce"]. Pass a list like
            ["ReadOnlyMany", "ReadWriteMany"] for ROX/RWX filesystems.
        storage_class: optional StorageClass name. If the cluster has no
            StorageClass at all, run `bootstrap_local_path_provisioner()`
            first to give it one.
        labels: optional labels.
    """
    if access_modes is None:
        access_modes = ["ReadWriteOnce"]
    md: dict = {"name": name, "namespace": namespace}
    if labels:
        md["labels"] = labels
    spec = {
        "accessModes": access_modes,
        "resources": {"requests": {"storage": size}},
    }
    if storage_class:
        spec["storageClassName"] = storage_class
    manifest = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": md,
        "spec": spec,
    }
    import yaml
    return generic.apply_yaml(yaml.safe_dump(manifest))


def bootstrap_local_path_provisioner(
    set_as_default: bool = True,
    apply_immediately: bool = True,
) -> str:
    """⚠️ WRITE — install Rancher local-path-provisioner in one shot.
    Solves "my cluster has no StorageClass, so PVCs sit Pending forever"
    on dev/test clusters (kind, k3s default, minikube with no extras).

    The manifest creates:
      - a privileged DaemonSet (`local-path-provisioner`) on every node
      - a `local-path` StorageClass with `volumeBindingMode=WaitForFirstConsumer`
      - the RBAC it needs

    PVCs submitted with `storage_class_name="local-path"` are then
    auto-provisioned onto node hostPath storage. Production clusters
    should NOT use this — hostPath PVCs are node-local and data is
    lost if the node dies.

    Args:
        set_as_default: when True (default), mark the StorageClass as the
            cluster's default so callers can omit `storage_class_name`.
            Pass False if you already have a default SC and want both
            to coexist.
        apply_immediately: when True (default), runs `apply_yaml` to
            create all the cluster resources. Pass False to return the
            raw YAML for the user to inspect before applying.

    Manifest URL:
        Override via `K8S_MCP_LOCAL_PATH_PROVISIONER_URL` for air-gapped
        clusters with an internal mirror.
    """
    _read_only_guard()
    yaml_text = _fetch_local_path_manifest()

    if not set_as_default:
        # Strip the default annotation so the cluster's existing default
        # stays default. The annotation's value form is YAML-string so we
        # match the exact key Rancher ships.
        yaml_text = yaml_text.replace(_DEFAULT_CLASS_ANNOTATION,
                                      _DEFAULT_CLASS_ANNOTATION.replace("true", "false"))

    if not apply_immediately:
        return (
            f"Local Path Provisioner manifest (NOT applied; "
            f"set_as_default={set_as_default}):\n"
            f"----\n{yaml_text}----\n"
            f"Re-run with apply_immediately=True to install."
        )

    result = generic.apply_yaml(yaml_text)
    sc_state = "default" if set_as_default else "non-default"
    return (
        f"{result}\n\n"
        f"Local Path Provisioner installed (StorageClass 'local-path', {sc_state}). "
        f"You can now create PVCs / StatefulSets with "
        f"storage_class_name='local-path' (or omit — {sc_state})."
    )


def _fetch_local_path_manifest() -> str:
    """Fetch + cache the local-path-provisioner manifest. Module-level
    cache survives within one MCP session; restarts re-fetch.

    Raises RuntimeError with an actionable message on network/parse
    failure — the cluster may be air-gapped; in that case the user
    can paste `K8S_MCP_LOCAL_PATH_PROVISIONER_URL` pointing at an
    internal mirror.
    """
    global _manifest_cache
    if _manifest_cache is not None:
        return _manifest_cache
    url = get_settings().local_path_provisioner_url
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "k8s-mcp/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(
            f"Could not fetch local-path-provisioner manifest from {url!r}: {e}. "
            f"If the cluster is air-gapped, set "
            f"K8S_MCP_LOCAL_PATH_PROVISIONER_URL=https://your-mirror/path/to/local-path-storage.yaml "
            f"and retry. Or pre-install manually with "
            f"`kubectl apply -f {url}` and skip this tool."
        ) from e
    if not text.strip():
        raise RuntimeError(
            f"local-path-provisioner manifest at {url!r} came back empty"
        )
    _manifest_cache = text
    return text


def register(mcp) -> None:
    mcp.tool()(create_pvc)
    mcp.tool()(bootstrap_local_path_provisioner)
