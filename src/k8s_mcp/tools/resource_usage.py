"""Resource-request auditor — static requests/limits compliance check.

Why a separate tool when `kubectl describe` shows resources? Because
describe shows one pod at a time. This tool sweeps a namespace /
cluster and surfaces the systematic risks: workloads without
requests (Burstable QoS, can land on any node + get evicted first),
workloads without limits (memory can be OOMKilled at any time,
CPU can starve neighbors), and the classic `limits < requests`
footgun that kube silently bumps to `requests`.

Read-only. Static-only on purpose — pulls nothing from
metrics-server / Prometheus. Companion tool to `diagnose_pod`
(runtime triage) — this one gives the static hygiene score.
"""
from __future__ import annotations

import logging

from kubernetes import dynamic
from kubernetes.dynamic.exceptions import ResourceNotFoundError

from ..client import get_api_client
from ..formatters import short_table
from . import generic as generic_mod

logger = logging.getLogger(__name__)

# Indirection so tests can monkeypatch without affecting other callers.
_generic = generic_mod


def _dyn_client() -> dynamic.DynamicClient:
    return dynamic.DynamicClient(get_api_client())


# ---------- extraction helpers ---------------------------------------------


def _extract_pod_containers(pod: dict) -> list[dict]:
    return (pod.get("spec") or {}).get("containers") or []


def _extract_workload_containers(workload: dict) -> list[dict]:
    spec = workload.get("spec") or {}
    return ((spec.get("template") or {}).get("spec") or {}).get(
        "containers") or []


def _has_resources(c: dict, key: str) -> bool:
    """A container is considered to have requests/limits iff the dict
    is present and non-empty. None and {} both → 'missing'."""
    res = c.get("resources") or {}
    val = res.get(key) or {}
    return bool(val)


def _per_container_rows(containers: list[dict]) -> list[dict]:
    """Render one row per container with compliance icons.

    `requests` and `limits` both default to ❌ if the dict is absent —
    a missing `limits` is a problem too (memory has no ceiling).
    """
    rows = []
    for c in containers:
        rows.append({
            "NAME": c.get("name", "?"),
            "REQUESTS": "✓" if _has_resources(c, "requests") else "❌",
            "LIMITS":   "✓" if _has_resources(c, "limits")   else "❌",
        })
    return rows


def _issue_kind(mode: str, c: dict) -> bool:
    if mode == "missing_requests":
        return not _has_resources(c, "requests")
    if mode == "missing_limits":
        return not _has_resources(c, "limits")
    if mode == "inconsistent":
        res = c.get("resources") or {}
        req = res.get("requests") or {}
        lim = res.get("limits") or {}
        # Compare item by item; "limits < requests" is the footgun.
        for k, v in req.items():
            if k in lim and str(lim[k]) != str(v):
                # Naive string compare; CPU "500m" < "100m" lexically,
                # but for the auditor we just flag any divergence.
                try:
                    if _cpu_or_mem_to_millicpus(lim[k]) < _cpu_or_mem_to_millicpus(v):
                        return True
                except ValueError:
                    return True
        return False
    return False


def _cpu_or_mem_to_millicpus(value) -> int:
    """Tiny parser — accepts '500m', '1', '2Gi', etc. Best-effort
    numeric comparison for audit. Unknown formats raise ValueError."""
    s = str(value).strip()
    if s.endswith("m"):
        return int(s[:-1])
    if s.endswith("Gi"):
        return int(s[:-2]) * 1024 * 1024
    if s.endswith("Mi"):
        return int(s[:-2]) * 1024
    if s.endswith("G"):
        return int(s[:-1]) * 1024 * 1024 * 1000
    if s.endswith("M"):
        return int(s[:-1]) * 1000
    return int(s)


# ---------- pod workloads / orphan filtering --------------------------------


def _is_owned_by_workload(pod: dict) -> bool:
    """Pods under a Deployment / StatefulSet / DaemonSet are covered by
    their parent's spec — auditing them as Pods duplicates the workload
    audit rows. Sibling-mode audits go via `kind=Pod & include_orphan=True`."""
    for owner in (pod.get("metadata") or {}).get("ownerReferences") or []:
        if (owner.get("kind") or "") in ("Deployment", "StatefulSet",
                                         "DaemonSet", "ReplicaSet",
                                         "Job", "CronJob"):
            return True
    return False


# ---------- main entry ------------------------------------------------------


_VALID_MODES = ("missing_requests", "missing_limits", "inconsistent")
_VALID_KINDS = ("Pod", "Deployment", "StatefulSet", "DaemonSet")


def analyze_resource_usage(
    namespace: str = "default",
    kind: str = "Pod",
    mode: str = "missing_requests",
) -> str:
    """📊 RESOURCE-USAGE static auditor — sweep a namespace for workload
    hygiene issues.

    Args:
        namespace: namespace to audit. None = cluster-scope for built-ins
            Pod / Deployment / StatefulSet / DaemonSet that have list_all.
        kind: `Pod` (default — flags orphan pods only; workload-owned pods
            are covered under their parent's `kind=`), or one of
            `Deployment` / `StatefulSet` / `DaemonSet` (audits the pod
            template's containers).
        mode:
            - `missing_requests` (default): containers with no
              `resources.requests` set. Burstable QoS — scheduler can
              place them anywhere, and they're first in line for eviction.
            - `missing_limits`: containers with no `resources.limits`
              set. Memory has no ceiling → arbitrary OOMKilled; CPU
              can throttle neighbors.
            - `inconsistent`: containers whose `limits < requests` for
              any resource. kube silently bumps limits to requests here,
              which usually means the manifest is wrong.

    Returns a multi-section report per scanned kind: the offending
    containers table, plus a footprint summary. Always read-only.

    Limits < requests comparison is best-effort numeric; weird suffixes
    (like `"500m"` vs `"0.5"`) are flagged conservatively and fall
    through to the `inconsistent` reporter regardless.
    """
    if mode not in _VALID_MODES:
        raise ValueError(
            f"mode must be one of {_VALID_MODES}, got {mode!r}"
        )
    if kind not in _VALID_KINDS:
        raise ValueError(
            f"kind must be one of {_VALID_KINDS}, got {kind!r}"
        )

    dc = _dyn_client()
    try:
        res = _generic._resource_for_kind(dc, kind, api_version=_api_version_for_kind(kind))
    except ResourceNotFoundError as e:
        raise RuntimeError(f"{kind} not available on this cluster") from e
    try:
        items = list(res.get(namespace=namespace).items) \
            if namespace else list(res.get().items)
    except TypeError:
        # cluster-scope kinds don't accept namespace; ignore.
        items = list(res.get().items)

    if not items:
        return f"## {kind} resource usage: namespace '{namespace}'\n(no {kind}s found)"

    rows = []
    for obj in items:
        meta = obj.get("metadata") or {}
        containers = (_extract_workload_containers(obj)
                      if kind != "Pod"
                      else _extract_pod_containers(obj))
        if kind == "Pod" and _is_owned_by_workload(obj):
            continue
        for c in containers:
            if _issue_kind(mode, c):
                rows.append({
                    "WORKLOAD": meta.get("name", "?"),
                    "CONTAINER": c.get("name", "?"),
                    "ISSUE": "❌",
                    "REQUESTS": str((c.get("resources") or {}).get("requests") or {}),
                    "LIMITS":   str((c.get("resources") or {}).get("limits") or {}),
                })

    if not rows:
        return (
            f"## {kind} resource usage: namespace '{namespace}' (mode={mode})\n"
            "✅ no issues found"
        )

    table = short_table(
        rows, ["WORKLOAD", "CONTAINER", "ISSUE", "REQUESTS", "LIMITS"],
    )
    return (
        f"## {kind} resource usage: namespace '{namespace}' (mode={mode})\n"
        f"{len(items)} {kind}(s) scanned, {len(rows)} containers with issues:\n\n"
        f"{table}"
    )


def _api_version_for_kind(kind: str) -> str:
    return {
        "Pod": "v1", "Deployment": "apps/v1", "StatefulSet": "apps/v1",
        "DaemonSet": "apps/v1",
    }[kind]


def register(mcp) -> None:
    mcp.tool()(analyze_resource_usage)
