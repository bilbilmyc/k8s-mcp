"""Read-only NVIDIA GPU / AI workload diagnostics.

These tools deliberately discover Kubernetes extended resources and NVIDIA GPU
Operator state instead of assuming a particular GPU SKU, MIG profile, or
Operator chart revision. They never mutate cluster objects.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from ..client import get_api_client
from ..formatters import short_table

_GPU_PREFIX = "nvidia.com/"
_GPU_OPERATOR_GROUP = "nvidia.com"
_GPU_OPERATOR_VERSION = "v1"
_GPU_OPERATOR_PLURAL = "clusterpolicies"
_DEFAULT_OPERATOR_NAMESPACE = "gpu-operator"
_MIG_RESOURCE_PREFIX = "nvidia.com/mig"
_MIG_LABEL_PREFIX = "nvidia.com/mig."
# DRA (Dynamic Resource Allocation) — GA as resource.k8s.io/v1 in Kubernetes
# 1.34, beta as v1beta1 in 1.32/1.33. Detection tries GA first and falls back
# so the same tool works across in-flight cluster upgrades.
_DRA_GROUP = "resource.k8s.io"
_DRA_VERSION_CANDIDATES = ("v1", "v1beta1")


def _core_v1():
    return client.CoreV1Api(get_api_client())


def _apps_v1():
    return client.AppsV1Api(get_api_client())


def _batch_v1():
    return client.BatchV1Api(get_api_client())


def _custom_objects():
    return client.CustomObjectsApi(get_api_client())


def _value(obj: Any, field: str, default: Any = None) -> Any:
    """Read a field from either a Kubernetes model or a dict fixture."""
    if isinstance(obj, dict):
        return obj.get(field, default)
    return getattr(obj, field, default)


def _items(result: Any) -> list[Any]:
    return list(_value(result, "items", []) or [])


def _metadata(obj: Any) -> Any:
    return _value(obj, "metadata", {}) or {}


def _name(obj: Any) -> str:
    return str(_value(_metadata(obj), "name", "<unknown>"))


def _namespace(obj: Any) -> str:
    return str(_value(_metadata(obj), "namespace", "default") or "default")


def _labels(obj: Any) -> dict[str, str]:
    labels = _value(_metadata(obj), "labels", {}) or {}
    return {str(key): str(value) for key, value in labels.items()}


def _resource_map(value: Any) -> dict[str, str]:
    value = value or {}
    if not isinstance(value, dict):
        return {}
    return {str(key): str(amount) for key, amount in value.items()}


def _gpu_resources(resources: Any) -> dict[str, str]:
    return {
        key: amount
        for key, amount in _resource_map(resources).items()
        if key.startswith(_GPU_PREFIX)
    }


def _mig_resources(resources: Any) -> dict[str, str]:
    """MIG slice resources (`nvidia.com/mig-<profile>`), not whole GPUs."""
    return {
        key: amount
        for key, amount in _resource_map(resources).items()
        if key.startswith(_MIG_RESOURCE_PREFIX)
    }


def _mig_labels(labels: Any) -> dict[str, str]:
    """NVIDIA MIG feature-discovery labels (`nvidia.com/mig.*`)."""
    return {
        str(key): str(value)
        for key, value in (labels or {}).items()
        if str(key).startswith(_MIG_LABEL_PREFIX)
    }


def _node_mig_signature(node: Any) -> bool:
    """True when a Node exposes MIG slice resources or announces MIG capability."""
    capacity, allocatable = _node_gpu_resources(node)
    return bool(
        _mig_resources(capacity)
        or _mig_resources(allocatable)
        or _mig_labels(_labels(node))
    )


def _quantity(value: str) -> float:
    """Parse an extended-resource quantity defensively for report totals."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _format_quantity(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:g}"


def _render_resources(resources: dict[str, str] | dict[str, float]) -> str:
    if not resources:
        return "-"
    return ", ".join(f"{key}={value}" for key, value in sorted(resources.items()))


def _sum_resources(target: dict[str, float], resources: dict[str, str]) -> None:
    for name, amount in resources.items():
        target[name] += _quantity(amount)


def _container_gpu_limits(container: Any) -> dict[str, str]:
    resources = _value(container, "resources", {}) or {}
    return _gpu_resources(_value(resources, "limits", {}) or {})


def _pod_gpu_limits(pod: Any) -> dict[str, str]:
    """Return effective GPU limits: app-container sum versus init max.

    Kubernetes schedules a Pod using the sum of regular containers and the
    maximum init-container request. GPU resources must be limits, so this
    mirrors that accounting without guessing at any vendor-specific profile.
    """
    spec = _value(pod, "spec", {}) or {}
    app_totals: dict[str, float] = defaultdict(float)
    init_max: dict[str, float] = defaultdict(float)
    for container in _value(spec, "containers", []) or []:
        for name, amount in _container_gpu_limits(container).items():
            app_totals[name] += _quantity(amount)
    for container in _value(spec, "init_containers", []) or []:
        for name, amount in _container_gpu_limits(container).items():
            init_max[name] = max(init_max[name], _quantity(amount))
    names = set(app_totals) | set(init_max)
    return {name: _format_quantity(max(app_totals[name], init_max[name])) for name in names}


def _node_gpu_resources(node: Any) -> tuple[dict[str, str], dict[str, str]]:
    status = _value(node, "status", {}) or {}
    return (
        _gpu_resources(_value(status, "capacity", {}) or {}),
        _gpu_resources(_value(status, "allocatable", {}) or {}),
    )


def _is_gpu_node(node: Any) -> bool:
    capacity, allocatable = _node_gpu_resources(node)
    labels = _labels(node)
    return bool(
        capacity
        or allocatable
        or labels.get("nvidia.com/gpu.present", "").lower() == "true"
        or "nvidia.com/gpu.product" in labels
    )


def _pod_phase(pod: Any) -> str:
    return str(_value(_value(pod, "status", {}) or {}, "phase", "Unknown") or "Unknown")


def _pod_node(pod: Any) -> str:
    return str(_value(_value(pod, "spec", {}) or {}, "node_name", "") or "(unscheduled)")


def _pod_unschedulable_message(pod: Any) -> str:
    status = _value(pod, "status", {}) or {}
    for condition in _value(status, "conditions", []) or []:
        if _value(condition, "type") != "PodScheduled":
            continue
        if str(_value(condition, "status", "")).lower() != "false":
            continue
        reason = _value(condition, "reason", "Unschedulable") or "Unschedulable"
        message = _value(condition, "message", "") or ""
        return f"{reason}: {message}".rstrip(": ")
    return ""


def _ready_state(pod: Any) -> str:
    status = _value(pod, "status", {}) or {}
    for condition in _value(status, "conditions", []) or []:
        if _value(condition, "type") == "Ready":
            return str(_value(condition, "status", "Unknown"))
    return "Unknown"


def _node_ready(node: Any) -> str:
    status = _value(node, "status", {}) or {}
    for condition in _value(status, "conditions", []) or []:
        if _value(condition, "type") == "Ready":
            return str(_value(condition, "status", "Unknown"))
    return "Unknown"


def _taints(node: Any) -> str:
    spec = _value(node, "spec", {}) or {}
    rendered = []
    for taint in _value(spec, "taints", []) or []:
        key = _value(taint, "key", "")
        value = _value(taint, "value", "")
        effect = _value(taint, "effect", "")
        rendered.append(f"{key}={value}:{effect}" if value else f"{key}:{effect}")
    return ", ".join(rendered) or "-"


def _list_gpu_pods(pods: list[Any]) -> list[Any]:
    return [pod for pod in pods if _pod_gpu_limits(pod)]


def _list_pods(core: Any, namespace: str | None = None, **kwargs: Any) -> list[Any]:
    if namespace:
        return _items(core.list_namespaced_pod(namespace, **kwargs))
    return _items(core.list_pod_for_all_namespaces(**kwargs))


def _operator_policy_summary() -> str:
    """Read ClusterPolicy best-effort; missing CRD is normal on non-Operator clusters."""
    try:
        result = _custom_objects().list_cluster_custom_object(
            group=_GPU_OPERATOR_GROUP,
            version=_GPU_OPERATOR_VERSION,
            plural=_GPU_OPERATOR_PLURAL,
        )
    except ApiException as exc:
        if exc.status in (403, 404):
            return "not available (ClusterPolicy CRD absent or RBAC denied)"
        return f"unavailable ({exc.status} {exc.reason or 'API error'})"
    except AttributeError:
        return "not available (CustomObjects API unavailable)"

    policies = result.get("items", []) if isinstance(result, dict) else []
    if not policies:
        return "not found"
    rendered = []
    for policy in policies:
        metadata = policy.get("metadata") or {}
        status = policy.get("status") or {}
        state = status.get("state") or status.get("status") or "Unknown"
        rendered.append(f"{metadata.get('name', 'cluster-policy')}={state}")
    return ", ".join(rendered)


def _cluster_policy_mig_strategy() -> str:
    """Read `spec.migManager.strategy` from the ClusterPolicy, best-effort.

    Missing CRD / RBAC is a normal non-Operator cluster and rendered as text,
    never raised — the same contract as `_operator_policy_summary`.
    """
    try:
        result = _custom_objects().list_cluster_custom_object(
            group=_GPU_OPERATOR_GROUP,
            version=_GPU_OPERATOR_VERSION,
            plural=_GPU_OPERATOR_PLURAL,
        )
    except ApiException as exc:
        if exc.status in (403, 404):
            return "not available"
        return f"unavailable ({exc.status})"
    except AttributeError:
        return "not available"

    policies = result.get("items", []) if isinstance(result, dict) else []
    strategies = sorted({
        str(((policy.get("spec") or {}).get("migManager") or {}).get("strategy") or "")
        for policy in policies
        if isinstance(policy, dict)
    } - {""})
    return ", ".join(strategies) if strategies else "unset"


def _operator_pod_rows(core: Any, namespace: str) -> tuple[list[dict[str, str]], str | None]:
    try:
        pods = _items(core.list_namespaced_pod(namespace))
    except ApiException as exc:
        if exc.status == 404:
            return [], f"namespace {namespace!r} not found"
        if exc.status == 403:
            return [], f"RBAC cannot list Pods in namespace {namespace!r}"
        return [], f"failed to list operator Pods: {exc.status} {exc.reason or 'API error'}"

    rows = []
    for pod in pods:
        name = _name(pod)
        lowered = name.lower()
        component = next(
            (
                candidate
                for candidate in ("device-plugin", "dcgm-exporter", "mig-manager", "validator", "gpu-feature-discovery")
                if candidate in lowered
            ),
            "operator",
        )
        rows.append(
            {
                "COMPONENT": component,
                "POD": name,
                "PHASE": _pod_phase(pod),
                "READY": _ready_state(pod),
                "NODE": _pod_node(pod),
            }
        )
    return rows, None


def _selector_from_object(workload: Any) -> str:
    spec = _value(workload, "spec", {}) or {}
    selector = _value(spec, "selector", {}) or {}
    labels = _value(selector, "match_labels", {}) or {}
    return ",".join(f"{key}={value}" for key, value in sorted(labels.items()))


def _template_gpu_limits(workload: Any) -> dict[str, str]:
    spec = _value(workload, "spec", {}) or {}
    template = _value(spec, "template", {}) or {}
    template_spec = _value(template, "spec", {}) or {}
    synthetic = {"spec": template_spec}
    return _pod_gpu_limits(synthetic)


def _read_workload(name: str, namespace: str, kind: str) -> tuple[Any, str]:
    normalized = kind.strip().lower()
    try:
        if normalized == "pod":
            return _core_v1().read_namespaced_pod(name, namespace), "Pod"
        if normalized == "deployment":
            return _apps_v1().read_namespaced_deployment(name, namespace), "Deployment"
        if normalized == "job":
            return _batch_v1().read_namespaced_job(name, namespace), "Job"
    except ApiException as exc:
        if exc.status == 404:
            raise ValueError(f"{kind} {namespace}/{name} not found") from exc
        raise RuntimeError(f"failed to read {kind} {namespace}/{name}: {exc.status} {exc.reason}") from exc
    raise ValueError("kind must be one of: Pod, Deployment, Job")


def gpu_cluster_overview() -> str:
    """🟢 NVIDIA GPU CLUSTER OVERVIEW — summarize discovered NVIDIA capacity and demand.

    Reads Node extended resources (including `nvidia.com/gpu` and dynamic
    `nvidia.com/mig-*` resources), non-terminal GPU Pods, and the NVIDIA GPU
    Operator ClusterPolicy when available. No NVIDIA components are installed
    or changed. The tool remains useful on a cluster without the GPU Operator:
    it reports the resources that Kubernetes actually exposes.
    """
    core = _core_v1()
    try:
        nodes = _items(core.list_node())
    except ApiException as exc:
        raise RuntimeError(f"failed to list Nodes: {exc.status} {exc.reason}") from exc

    gpu_nodes = [node for node in nodes if _is_gpu_node(node)]
    capacity: dict[str, float] = defaultdict(float)
    allocatable: dict[str, float] = defaultdict(float)
    rows = []
    for node in gpu_nodes:
        node_capacity, node_allocatable = _node_gpu_resources(node)
        _sum_resources(capacity, node_capacity)
        _sum_resources(allocatable, node_allocatable)
        rows.append(
            {
                "NODE": _name(node),
                "READY": _node_ready(node),
                "SCHEDULABLE": "no" if _value(_value(node, "spec", {}) or {}, "unschedulable", False) else "yes",
                "CAPACITY": _render_resources(node_capacity),
                "ALLOCATABLE": _render_resources(node_allocatable),
            }
        )

    lines = ["## NVIDIA GPU cluster overview"]
    lines.append(f"GPU nodes discovered: {len(gpu_nodes)} / {len(nodes)}")
    lines.append(f"ClusterPolicy: {_operator_policy_summary()}")
    lines.append("\n### Node capacity")
    lines.append(short_table(rows, ["NODE", "READY", "SCHEDULABLE", "CAPACITY", "ALLOCATABLE"]))
    lines.append(f"\nTotal capacity: {_render_resources({key: _format_quantity(value) for key, value in capacity.items()})}")
    lines.append(f"Total allocatable: {_render_resources({key: _format_quantity(value) for key, value in allocatable.items()})}")

    try:
        gpu_pods = [pod for pod in _list_gpu_pods(_list_pods(core)) if _pod_phase(pod) not in {"Succeeded", "Failed"}]
    except ApiException as exc:
        lines.append(f"\n### GPU workload demand\nUnavailable: {exc.status} {exc.reason or 'API error'}")
        return "\n".join(lines)

    requested: dict[str, float] = defaultdict(float)
    phases = Counter()
    for pod in gpu_pods:
        _sum_resources(requested, _pod_gpu_limits(pod))
        phases[_pod_phase(pod)] += 1
    lines.append("\n### GPU workload demand")
    lines.append(f"Active GPU Pods: {len(gpu_pods)} ({', '.join(f'{phase}={count}' for phase, count in sorted(phases.items())) or 'none'})")
    lines.append(f"Requested GPU limits: {_render_resources({key: _format_quantity(value) for key, value in requested.items()})}")
    return "\n".join(lines)


def gpu_node_inspect(name: str) -> str:
    """🔍 INSPECT NVIDIA GPU NODE — show GPU resources, labels, taints, and GPU Pods on one Node.

    The report dynamically includes all `nvidia.com/*` extended resources, so
    MIG profiles work without a hard-coded profile list. It is read-only.

    Args:
        name: Kubernetes Node name.
    """
    core = _core_v1()
    try:
        node = core.read_node(name)
    except ApiException as exc:
        if exc.status == 404:
            raise ValueError(f"Node {name!r} not found") from exc
        raise RuntimeError(f"failed to read Node {name!r}: {exc.status} {exc.reason}") from exc

    capacity, allocatable = _node_gpu_resources(node)
    nvidia_labels = {key: value for key, value in _labels(node).items() if key.startswith(_GPU_PREFIX)}
    lines = [f"## NVIDIA GPU node {name}"]
    lines.append(f"Ready: {_node_ready(node)}")
    lines.append(f"Schedulable: {'no' if _value(_value(node, 'spec', {}) or {}, 'unschedulable', False) else 'yes'}")
    lines.append(f"Taints: {_taints(node)}")
    lines.append(f"Capacity: {_render_resources(capacity)}")
    lines.append(f"Allocatable: {_render_resources(allocatable)}")
    lines.append("\n### NVIDIA labels")
    lines.append(short_table([{"LABEL": key, "VALUE": value} for key, value in sorted(nvidia_labels.items())], ["LABEL", "VALUE"]))

    try:
        pods = _list_gpu_pods(_list_pods(core, field_selector=f"spec.nodeName={name}"))
    except ApiException as exc:
        lines.append(f"\n### GPU Pods\nUnavailable: {exc.status} {exc.reason or 'API error'}")
        return "\n".join(lines)
    rows = [
        {
            "NAMESPACE": _namespace(pod),
            "POD": _name(pod),
            "PHASE": _pod_phase(pod),
            "GPU_LIMITS": _render_resources(_pod_gpu_limits(pod)),
        }
        for pod in pods
    ]
    lines.append("\n### GPU Pods")
    lines.append(short_table(rows, ["NAMESPACE", "POD", "PHASE", "GPU_LIMITS"]))
    return "\n".join(lines)


def gpu_workload_inspect(name: str, namespace: str = "default", kind: str = "Pod") -> str:
    """🔍 INSPECT NVIDIA GPU WORKLOAD — explain GPU allocation and scheduling for a Pod, Deployment, or Job.

    Args:
        name: workload name.
        namespace: workload namespace.
        kind: one of `Pod`, `Deployment`, or `Job`.

    For Pod, reports live placement and scheduler feedback. For Deployment and
    Job, reports the GPU limits declared by the Pod template plus matching Pods
    when a label selector is available. This tool does not read container
    environment variables or execute into workloads.
    """
    workload, display_kind = _read_workload(name, namespace, kind)
    core = _core_v1()
    lines = [f"## {display_kind} {namespace}/{name} — NVIDIA GPU inspection"]

    if display_kind == "Pod":
        limits = _pod_gpu_limits(workload)
        lines.append(f"Phase: {_pod_phase(workload)} | Node: {_pod_node(workload)} | GPU limits: {_render_resources(limits)}")
        unschedulable = _pod_unschedulable_message(workload)
        if unschedulable:
            lines.append(f"Scheduler verdict: {unschedulable}")
        elif _pod_phase(workload) == "Pending":
            lines.append("Scheduler verdict: Pending without a PodScheduled=False condition; inspect events and init/image status.")
        return "\n".join(lines)

    template_limits = _template_gpu_limits(workload)
    selector = _selector_from_object(workload)
    lines.append(f"Template GPU limits: {_render_resources(template_limits)}")
    if not selector:
        lines.append("Matching Pods: unavailable because this workload has no matchLabels selector.")
        return "\n".join(lines)

    try:
        pods = _list_pods(core, namespace, label_selector=selector)
    except ApiException as exc:
        lines.append(f"Matching Pods: unavailable: {exc.status} {exc.reason or 'API error'}")
        return "\n".join(lines)
    rows = [
        {
            "POD": _name(pod),
            "PHASE": _pod_phase(pod),
            "NODE": _pod_node(pod),
            "GPU_LIMITS": _render_resources(_pod_gpu_limits(pod)),
            "SCHEDULER": _pod_unschedulable_message(pod) or "-",
        }
        for pod in _list_gpu_pods(pods)
    ]
    lines.append("\n### Matching GPU Pods")
    lines.append(short_table(rows, ["POD", "PHASE", "NODE", "GPU_LIMITS", "SCHEDULER"]))
    return "\n".join(lines)


def gpu_pending_workloads(namespace: str | None = None, limit: int = 50) -> str:
    """⚠️ LIST PENDING NVIDIA GPU WORKLOADS — group GPU Pods waiting for scheduling.

    Args:
        namespace: optional namespace. Omit to inspect all namespaces allowed by RBAC.
        limit: maximum number of rows (1-200, default 50).

    Only Pods with `phase=Pending` and an `nvidia.com/*` GPU limit are
    returned. The scheduler message is reported verbatim enough to distinguish
    capacity, taints, affinity, and MIG-profile mismatches.
    """
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    core = _core_v1()
    try:
        pods = _list_pods(core, namespace, field_selector="status.phase=Pending")
    except ApiException as exc:
        scope = namespace or "all namespaces"
        raise RuntimeError(f"failed to list pending Pods in {scope}: {exc.status} {exc.reason}") from exc

    gpu_pods = _list_gpu_pods(pods)
    rows = []
    for pod in gpu_pods[:limit]:
        rows.append(
            {
                "NAMESPACE": _namespace(pod),
                "POD": _name(pod),
                "GPU_LIMITS": _render_resources(_pod_gpu_limits(pod)),
                "REASON": _pod_unschedulable_message(pod) or "Pending; no scheduler condition",
            }
        )
    scope = namespace or "all namespaces"
    lines = [f"## Pending NVIDIA GPU workloads ({scope})", f"Found: {len(gpu_pods)}; shown: {len(rows)}"]
    lines.append(short_table(rows, ["NAMESPACE", "POD", "GPU_LIMITS", "REASON"]))
    if len(gpu_pods) > len(rows):
        lines.append(f"Truncated at limit={limit}; rerun with a higher limit (max 200).")
    return "\n".join(lines)


def gpu_diagnose(operator_namespace: str = _DEFAULT_OPERATOR_NAMESPACE) -> str:
    """🩺 DIAGNOSE NVIDIA GPU — one-shot health check for GPU Operator and scheduling prerequisites.

    Checks NVIDIA GPU Nodes, allocatable extended resources, Pending GPU Pods,
    the optional GPU Operator ClusterPolicy, and Pods in `operator_namespace`.
    Missing GPU Operator CRDs are reported as a diagnostic finding rather than
    an error, because NVIDIA resources can be installed by another mechanism.

    Args:
        operator_namespace: namespace that runs the NVIDIA GPU Operator (default `gpu-operator`).
    """
    core = _core_v1()
    findings: list[tuple[str, str]] = []
    try:
        nodes = _items(core.list_node())
    except ApiException as exc:
        raise RuntimeError(f"failed to list Nodes: {exc.status} {exc.reason}") from exc

    gpu_nodes = [node for node in nodes if _is_gpu_node(node)]
    if not gpu_nodes:
        findings.append(("WARN", "No NVIDIA GPU Nodes were discovered. Check node labels, drivers, and Device Plugin registration."))
    else:
        unavailable = [node for node in gpu_nodes if not _node_gpu_resources(node)[1]]
        if unavailable:
            findings.append(("WARN", f"{len(unavailable)} GPU Node(s) expose no allocatable nvidia.com/* resources: {', '.join(_name(node) for node in unavailable)}."))
        not_ready = [node for node in gpu_nodes if _node_ready(node) != "True"]
        if not_ready:
            findings.append(("WARN", f"GPU Nodes not Ready: {', '.join(_name(node) for node in not_ready)}."))
        if not unavailable and not not_ready:
            findings.append(("OK", f"{len(gpu_nodes)} GPU Node(s) expose allocatable NVIDIA resources and are Ready."))

    policy = _operator_policy_summary()
    if policy.startswith("not available"):
        findings.append(("INFO", f"ClusterPolicy: {policy}. This is normal when the GPU Operator is not installed or RBAC is minimal."))
    elif policy == "not found":
        findings.append(("WARN", "NVIDIA GPU Operator ClusterPolicy was not found."))
    else:
        findings.append(("INFO", f"ClusterPolicy: {policy}."))

    operator_rows, operator_error = _operator_pod_rows(core, operator_namespace)
    if operator_error:
        findings.append(("INFO", f"Operator Pods: {operator_error}."))
    elif not operator_rows:
        findings.append(("WARN", f"No Pods found in operator namespace {operator_namespace!r}."))
    else:
        unhealthy = [row for row in operator_rows if row["PHASE"] != "Running" or row["READY"] != "True"]
        if unhealthy:
            findings.append(("WARN", f"{len(unhealthy)} GPU Operator Pod(s) are not Ready."))
        else:
            findings.append(("OK", f"{len(operator_rows)} Pod(s) in {operator_namespace!r} are Running and Ready."))

    try:
        pending = _list_gpu_pods(_list_pods(core, field_selector="status.phase=Pending"))
    except ApiException as exc:
        findings.append(("INFO", f"Pending GPU workload check unavailable: {exc.status} {exc.reason or 'API error'}."))
        pending = []
    if pending:
        findings.append(("WARN", f"{len(pending)} Pending GPU Pod(s); call gpu_pending_workloads for scheduler messages."))
    elif gpu_nodes:
        findings.append(("OK", "No Pending GPU Pods detected."))

    lines = ["## NVIDIA GPU diagnosis", "### Findings"]
    lines.extend(f"- **{level}** — {message}" for level, message in findings)
    lines.append("\n### GPU Operator Pods")
    if operator_error:
        lines.append(operator_error)
    else:
        lines.append(short_table(operator_rows, ["COMPONENT", "POD", "PHASE", "READY", "NODE"]))
    if pending:
        lines.append("\n### Pending GPU Pods")
        rows = [
            {
                "NAMESPACE": _namespace(pod),
                "POD": _name(pod),
                "GPU_LIMITS": _render_resources(_pod_gpu_limits(pod)),
                "REASON": _pod_unschedulable_message(pod) or "Pending; no scheduler condition",
            }
            for pod in pending[:10]
        ]
        lines.append(short_table(rows, ["NAMESPACE", "POD", "GPU_LIMITS", "REASON"]))
    return "\n".join(lines)


def gpu_mig_overview() -> str:
    """🧩 INSPECT NVIDIA MIG — cluster-wide Multi-Instance GPU inventory and demand.

    Read-only discovery of MIG state: slice resources (`nvidia.com/mig-*`),
    MIG feature-discovery labels, the GPU Operator `migManager.strategy` when
    a ClusterPolicy exists, per-node slice capacity, which Pods consume which
    profiles, and Pending Pods whose requested profile no node provides.

    With `mig.strategy=single` the device plugin exposes slices as plain
    `nvidia.com/gpu`, so per-slice identity is not visible through the
    Kubernetes API; the strategy line is the hint to interpret those numbers.

    DRA-claimed accelerators (ResourceSlice/ResourceClaim) are covered by
    `gpu_dra_overview` instead.
    """
    core = _core_v1()
    try:
        nodes = _items(core.list_node())
    except ApiException as exc:
        raise RuntimeError(f"failed to list Nodes: {exc.status} {exc.reason}") from exc

    mig_nodes = [node for node in nodes if _node_mig_signature(node)]
    label_strategies = sorted({
        _labels(node).get("nvidia.com/mig.strategy", "")
        for node in mig_nodes
        if _labels(node).get("nvidia.com/mig.strategy")
    })

    lines = ["## NVIDIA MIG overview"]
    lines.append(f"Nodes with MIG resources/labels: {len(mig_nodes)} / {len(nodes)}")
    lines.append(f"MIG strategy — ClusterPolicy: {_cluster_policy_mig_strategy()}"
                 + (f" | node labels: {', '.join(label_strategies)}" if label_strategies else ""))

    if not mig_nodes:
        lines.append(
            "\nMIG is not in use: no `nvidia.com/mig-*` resources and no `nvidia.com/mig.*` "
            "labels were found. Enable MIG via the GPU Operator migManager (or nvidia-mig-parted) "
            "before workloads can request slice profiles."
        )
        return "\n".join(lines)

    capacity: dict[str, float] = defaultdict(float)
    allocatable: dict[str, float] = defaultdict(float)
    rows = []
    for node in mig_nodes:
        node_capacity, node_allocatable = _node_gpu_resources(node)
        node_capacity, node_allocatable = _mig_resources(node_capacity), _mig_resources(node_allocatable)
        _sum_resources(capacity, node_capacity)
        _sum_resources(allocatable, node_allocatable)
        rows.append(
            {
                "NODE": _name(node),
                "READY": _node_ready(node),
                "STRATEGY": _labels(node).get("nvidia.com/mig.strategy", "-"),
                "MIG_CAPACITY": _render_resources(node_capacity),
                "MIG_ALLOCATABLE": _render_resources(node_allocatable),
            }
        )
    lines.append("\n### MIG nodes")
    lines.append(short_table(rows, ["NODE", "READY", "STRATEGY", "MIG_CAPACITY", "MIG_ALLOCATABLE"]))

    findings: list[tuple[str, str]] = []
    try:
        gpu_pods = [pod for pod in _list_gpu_pods(_list_pods(core)) if _pod_phase(pod) not in {"Succeeded", "Failed"}]
    except ApiException as exc:
        findings.append(("INFO", f"MIG demand check unavailable: {exc.status} {exc.reason or 'API error'}."))
        gpu_pods = []

    consumers = [pod for pod in gpu_pods if _mig_resources(_pod_gpu_limits(pod))]
    requested: dict[str, float] = defaultdict(float)
    for pod in consumers:
        _sum_resources(requested, _mig_resources(_pod_gpu_limits(pod)))
    lines.append("\n### MIG slice demand")
    lines.append(f"Pods holding MIG slices: {len(consumers)}")
    lines.append(f"Requested MIG limits: {_render_resources({k: _format_quantity(v) for k, v in requested.items()})}")

    deficits = {
        profile: _format_quantity(amount - allocatable.get(profile, 0.0))
        for profile, amount in requested.items()
        if amount > allocatable.get(profile, 0.0)
    }
    if deficits:
        rendered = ", ".join(f"{profile} over by {amount}" for profile, amount in sorted(deficits.items()))
        findings.append(("WARN", f"Requested MIG slices exceed cluster allocatable: {rendered}."))
    else:
        findings.append(("OK", "Requested MIG slices fit within cluster allocatable capacity."))

    pending = [
        pod for pod in gpu_pods
        if _pod_phase(pod) == "Pending" and _mig_resources(_pod_gpu_limits(pod))
    ]
    if pending:
        names = ", ".join(_namespace(pod) + "/" + _name(pod) for pod in pending[:5])
        findings.append(("WARN", f"{len(pending)} Pending Pod(s) request MIG slices: {names}. "
                                "A Pending Pod whose profile no node advertises can never schedule."))
    lines.append("\n### Findings")
    lines.extend(f"- **{level}** — {message}" for level, message in findings)
    return "\n".join(lines)


def _dra_list(custom: Any, version: str, plural: str, *, namespaced: bool = False) -> Any:
    if namespaced:
        return custom.list_custom_object_for_all_namespaces(
            group=_DRA_GROUP, version=version, plural=plural,
        )
    return custom.list_cluster_custom_object(
        group=_DRA_GROUP, version=version, plural=plural,
    )


def _detect_dra_version(custom: Any) -> tuple[str | None, str]:
    """Probe resource.k8s.io, preferring the GA group-version. Returns
    (version | None, human-readable status) without raising on absence."""
    last_error: ApiException | None = None
    for version in _DRA_VERSION_CANDIDATES:
        try:
            _dra_list(custom, version, "deviceclasses")
        except ApiException as exc:
            last_error = exc
            continue
        except AttributeError:
            return None, "not available (CustomObjects API unavailable)"
        return version, "available"
    if last_error is not None and last_error.status == 403:
        return None, ("not available (RBAC denied; grant get/list on resource.k8s.io "
                      "deviceclasses, resourceslices, resourceclaims)")
    if last_error is not None and last_error.status == 404:
        return None, ("not available (resource.k8s.io API group absent — DRA disabled "
                      "or Kubernetes < 1.32)")
    status = last_error.status if last_error is not None else "?"
    reason = last_error.reason if last_error is not None else "API error"
    return None, f"unavailable ({status} {reason or 'API error'})"


def gpu_dra_overview() -> str:
    """🧩 INSPECT DRA RESOURCES — read-only Dynamic Resource Allocation discovery for accelerators.

    Reports the `resource.k8s.io` state relevant to GPU workloads:
    DeviceClasses (with their drivers), per-driver ResourceSlice device
    inventory, and ResourceClaims with allocation / reservation status.
    Discovery only — the tool never mutates claims, slices, or CRDs, and
    falls back from the GA `v1` API to `v1beta1` on older clusters.

    Classic extended resources (`nvidia.com/gpu`, `nvidia.com/mig-*`) handed
    out by the device plugin are covered by `gpu_cluster_overview` and
    `gpu_mig_overview` instead.
    """
    custom = _custom_objects()
    version, status = _detect_dra_version(custom)
    lines = ["## Dynamic Resource Allocation (DRA) overview"]

    if version is None:
        lines.append(f"DRA API: {status}")
        lines.append(
            "\nDRA is where vendors advertise accelerators as ResourceSlices and "
            "workloads consume them via ResourceClaims. While absent, GPUs are "
            "allocated through the classic device-plugin extended resources shown "
            "by gpu_cluster_overview / gpu_mig_overview."
        )
        return "\n".join(lines)

    findings: list[tuple[str, str]] = []
    lines.append(f"DRA API version in use: {version}")

    class_rows = []
    for cls_ in _items(_dra_list(custom, version, "deviceclasses")):
        spec = _value(cls_, "spec", {}) or {}
        drivers = ", ".join(sorted({
            str(driver.get("name"))
            for driver in (spec.get("drivers") or [])
            if isinstance(driver, dict) and driver.get("name")
        })) or "-"
        class_rows.append({"DEVICECLASS": _name(cls_), "DRIVERS": drivers})
    lines.append("\n### DeviceClasses")
    lines.append(short_table(class_rows, ["DEVICECLASS", "DRIVERS"]) if class_rows else "None found.")

    per_driver: dict[str, dict[str, Any]] = {}
    for slice_ in _items(_dra_list(custom, version, "resourceslices")):
        spec = _value(slice_, "spec", {}) or {}
        driver = str(spec.get("driver") or "<unknown>")
        agg = per_driver.setdefault(driver, {"slices": 0, "devices": 0, "pools": set(), "nodes": set()})
        agg["slices"] += 1
        agg["devices"] += len(spec.get("devices") or [])
        pool = spec.get("pool") or {}
        if pool.get("name"):
            agg["pools"].add(str(pool["name"]))
        if spec.get("nodeName"):
            agg["nodes"].add(str(spec["nodeName"]))
    lines.append("\n### ResourceSlices (advertised devices)")
    if per_driver:
        slice_rows = [
            {
                "DRIVER": driver,
                "SLICES": str(agg["slices"]),
                "DEVICES": str(agg["devices"]),
                "POOLS": str(len(agg["pools"])),
                "NODES": str(len(agg["nodes"])) if agg["nodes"] else "-",
            }
            for driver, agg in sorted(per_driver.items())
        ]
        lines.append(short_table(slice_rows, ["DRIVER", "SLICES", "DEVICES", "POOLS", "NODES"]))
    else:
        lines.append("None found.")
        findings.append(("WARN", "DRA API exists but no driver has published ResourceSlices; "
                                 "DRA claims cannot be satisfied without a vendor driver."))

    claims = _items(_dra_list(custom, version, "resourceclaims", namespaced=True))
    claim_rows = []
    waiting = 0
    for claim in sorted(claims, key=lambda c: (_namespace(c), _name(c))):
        claim_status = _value(claim, "status", {}) or {}
        allocation = claim_status.get("allocation")
        results = ((allocation or {}).get("devices") or {}).get("results") or []
        drivers = ", ".join(sorted({
            str(result.get("driver"))
            for result in results
            if isinstance(result, dict) and result.get("driver")
        })) or "-"
        consumers = [
            str(entry.get("requestor") or entry.get("name") or entry.get("uid") or "<consumer>")
            for entry in (claim_status.get("reservedFor") or [])
            if isinstance(entry, dict)
        ]
        reserved = ", ".join(consumers[:2]) + (f" (+{len(consumers) - 2})" if len(consumers) > 2 else "")
        if not allocation and consumers:
            waiting += 1
        claim_rows.append(
            {
                "NAMESPACE": _namespace(claim),
                "CLAIM": _name(claim),
                "ALLOCATED": "yes" if allocation else "no",
                "DEVICES": str(len(results)) if allocation else "-",
                "DRIVERS": drivers,
                "RESERVED_BY": reserved or "-",
            }
        )
    lines.append("\n### ResourceClaims")
    lines.append(short_table(claim_rows, ["NAMESPACE", "CLAIM", "ALLOCATED", "DEVICES", "DRIVERS", "RESERVED_BY"]) if claim_rows else "None found.")

    if claims:
        if waiting:
            findings.append(("WARN", f"{waiting} ResourceClaim(s) are reserved by Pods but not yet allocated; "
                                     "check the DRA driver and ResourceSlice availability."))
        else:
            findings.append(("OK", "All ResourceClaims are allocated."))
        findings.append(("INFO", f"Claims found: {len(claims)}; call gpu_workload_inspect for the consuming Pods' placement."))
    elif per_driver:
        findings.append(("OK", "Slices are advertised but no workload claims exist yet."))

    lines.append("\n### Findings")
    lines.extend(f"- **{level}** — {message}" for level, message in findings)
    return "\n".join(lines)


def register(mcp) -> None:
    mcp.tool()(gpu_cluster_overview)
    mcp.tool()(gpu_node_inspect)
    mcp.tool()(gpu_workload_inspect)
    mcp.tool()(gpu_pending_workloads)
    mcp.tool()(gpu_diagnose)
    mcp.tool()(gpu_mig_overview)
    mcp.tool()(gpu_dra_overview)
