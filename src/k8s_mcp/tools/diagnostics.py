"""Pod-centric diagnostics — one-shot triage aggregators.

中文说明：
- `diagnose_pod`：拿一个 Pod 名，按 phase 自动分派出一份分层报告。
  Pending 走调度诊断（突出调度器自己的 Unschedulable 裁决 + PVC 绑定 +
  requests 汇总，不重算每节点拟合——那是重复调度器且易错），Running /
  CrashLoop 走运行时诊断（容器 state/lastState、OOMKilled、exit code、
  restart，CrashLoop 时自动 tail previous 容器日志）。全只读。
"""
from __future__ import annotations

from kubernetes import client
from kubernetes.client.rest import ApiException

from ..client import get_api_client
from ..formatters import format_age, short_table
from . import events as events_mod
from . import logs as logs_mod

# How many lines of the previous container log to tail for a crashing
# container. Enough to show a stack trace / fatal line without flooding.
_PREV_LOG_TAIL = 20
# How many recent events to surface at the bottom of the report.
_EVENTS_LIMIT = 10


def _core_v1():
    return client.CoreV1Api(get_api_client())


def _describe_state(state) -> tuple[str, str, str]:
    """Flatten a V1ContainerState to (STATE, REASON, EXIT) strings."""
    if state is None:
        return ("Unknown", "", "")
    if state.running is not None:
        return ("Running", "", "")
    if state.waiting is not None:
        return ("Waiting", state.waiting.reason or "", "")
    if state.terminated is not None:
        t = state.terminated
        exit_code = "" if t.exit_code is None else str(t.exit_code)
        return ("Terminated", t.reason or "", exit_code)
    return ("Unknown", "", "")


def _container_status_rows(pod) -> list[dict]:
    """Build the container-status table (init containers first, marked)."""
    rows: list[dict] = []
    st = pod.status
    for is_init, statuses in (
        (True, st.init_container_statuses or []),
        (False, st.container_statuses or []),
    ):
        for cs in statuses:
            state, reason, exit_code = _describe_state(cs.state)
            rows.append({
                "CONTAINER": (f"(init) {cs.name}" if is_init else cs.name),
                "READY": "yes" if cs.ready else "no",
                "STATE": state,
                "RESTARTS": str(cs.restart_count or 0),
                "REASON": reason,
                "EXIT": exit_code,
            })
    return rows


def _analyze_containers(pod) -> tuple[list[str], list[str]]:
    """Inspect container states for problems.

    Returns (problem_lines, containers_needing_previous_logs).
    """
    problems: list[str] = []
    logs_needed: list[str] = []
    st = pod.status
    all_statuses = list(st.init_container_statuses or []) + list(
        st.container_statuses or []
    )

    for cs in all_statuses:
        state = cs.state
        waiting = state.waiting if state else None
        terminated = state.terminated if state else None

        if waiting is not None and waiting.reason:
            msg = f" — {waiting.message}" if waiting.message else ""
            problems.append(
                f"⚠️ container '{cs.name}' waiting: {waiting.reason}{msg}"
            )
            if waiting.reason == "CrashLoopBackOff" and cs.name not in logs_needed:
                logs_needed.append(cs.name)

        if terminated is not None and terminated.exit_code not in (0, None):
            problems.append(
                f"⚠️ container '{cs.name}' terminated: "
                f"{terminated.reason or '?'} (exit {terminated.exit_code})"
            )
            if cs.name not in logs_needed:
                logs_needed.append(cs.name)

        # last_state explains WHY it restarted (the current state may just
        # be a healthy Running after the Nth restart).
        last = cs.last_state
        last_term = last.terminated if last else None
        if last_term is not None and cs.restart_count:
            line = (
                f"⚠️ container '{cs.name}' restarted {cs.restart_count}× — "
                f"last exit {last_term.exit_code} ({last_term.reason or '?'})"
            )
            if last_term.reason == "OOMKilled":
                line += "  ← OOM: raise the memory limit or fix the leak"
            problems.append(line)
            if cs.name not in logs_needed:
                logs_needed.append(cs.name)

    return problems, logs_needed


def _pvc_binding_lines(pod, core) -> list[str]:
    """Check each PVC volume's binding status — a common Pending cause."""
    lines: list[str] = []
    ns = pod.metadata.namespace
    for vol in (pod.spec.volumes or []):
        pvc_ref = vol.persistent_volume_claim
        if pvc_ref is None:
            continue
        claim = pvc_ref.claim_name
        try:
            pvc = core.read_namespaced_persistent_volume_claim(claim, ns)
            phase = (pvc.status.phase or "Unknown")
        except ApiException as e:
            lines.append(f"  PVC {claim} → read failed: {e.status} {e.reason}")
            continue
        marker = "" if phase == "Bound" else "  ⚠️ not bound"
        lines.append(f"  PVC {claim} → {phase}{marker}")
    return lines


def _pod_requests_summary(pod) -> str:
    """Sum CPU / memory requests across containers for context."""
    cpu_parts: list[str] = []
    mem_parts: list[str] = []
    for c in (pod.spec.containers or []):
        req = (c.resources.requests if c.resources else None) or {}
        if req.get("cpu"):
            cpu_parts.append(str(req["cpu"]))
        if req.get("memory"):
            mem_parts.append(str(req["memory"]))
    if not cpu_parts and not mem_parts:
        return "  requests: (none set — pod requests 0 CPU / 0 memory)"
    cpu = ", ".join(cpu_parts) if cpu_parts else "0"
    mem = ", ".join(mem_parts) if mem_parts else "0"
    return f"  requests: cpu=[{cpu}] memory=[{mem}]"


def _scheduling_lines(pod, core) -> list[str]:
    """Surface the scheduler's own verdict + PVC binding + requests.

    We deliberately do NOT recompute per-node fit; the kube-scheduler
    already writes an authoritative message like
    `0/3 nodes are available: 3 Insufficient cpu` into the PodScheduled
    condition, and re-deriving it here would duplicate (and risk
    contradicting) the scheduler.
    """
    lines: list[str] = []
    unsched = None
    for cond in (pod.status.conditions or []):
        if cond.type == "PodScheduled" and cond.status != "True":
            unsched = cond.message or cond.reason or "Unschedulable"
            break
    if unsched:
        lines.append(f"❌ Unschedulable: {unsched}")
    else:
        lines.append(
            "Pod is scheduled but still Pending — likely image pull or "
            "init container; see Containers / events below."
        )
    lines.extend(_pvc_binding_lines(pod, core))
    lines.append(_pod_requests_summary(pod))
    return lines


def _previous_logs_section(name, namespace, container) -> str:
    """Tail the previous container's logs (best-effort)."""
    try:
        out = logs_mod.get_pod_logs(
            pod_name=name,
            namespace=namespace,
            container=container,
            tail_lines=_PREV_LOG_TAIL,
            previous=True,
        )
    except Exception as e:  # noqa: BLE001 — diagnostics must not hard-fail
        return f"(previous logs unavailable: {type(e).__name__}: {e})"
    return out.strip() or "(previous container produced no logs)"


def diagnose_pod(name: str, namespace: str = "default") -> str:
    """🔍 DIAGNOSE POD — one-shot triage for a single Pod.

    Aggregates the ~5 calls an agent otherwise makes serially (get pod,
    read container statuses, list events, tail previous logs, check PVCs)
    into a single layered report, dispatched on the Pod's phase:

    - **Pending** → scheduling diagnosis: the scheduler's own
      `Unschedulable` verdict (not recomputed here), PVC binding status,
      and the pod's resource requests for context.
    - **Running / CrashLoopBackOff / Error** → runtime diagnosis: per
      container `state` + `lastState` (CrashLoopBackOff / ImagePullBackOff
      / OOMKilled / non-zero exit code), restart counts, and an automatic
      tail of the *previous* container's logs for anything crash-looping.
    - **Succeeded / Failed** → terminal-state summary.

    Always read-only — no mutations, safe to call repeatedly.

    Args:
        name: pod name.
        namespace: pod namespace (default "default").

    Returns a multi-section markdown report (## Pod / ## Containers /
    ## Diagnosis / ## Previous logs / ## Recent events). Raises
    ValueError if the pod does not exist.
    """
    core = _core_v1()
    try:
        pod = core.read_namespaced_pod(name=name, namespace=namespace)
    except ApiException as e:
        if e.status == 404:
            raise ValueError(f"pod {namespace}/{name} not found") from e
        raise RuntimeError(
            f"failed to read pod {namespace}/{name}: {e.status} {e.reason}"
        ) from e

    phase = pod.status.phase or "Unknown"
    node = pod.spec.node_name or "(unscheduled)"
    age = format_age(pod.metadata.creation_timestamp)

    sections: list[str] = [
        f"## Pod {namespace}/{name}",
        f"Phase: {phase} | Node: {node} | Age: {age}",
    ]

    # Container status table (skip when Pending-unscheduled: no statuses yet).
    rows = _container_status_rows(pod)
    if rows:
        sections.append("\n## Containers")
        sections.append(short_table(
            rows,
            ["CONTAINER", "READY", "STATE", "RESTARTS", "REASON", "EXIT"],
        ))

    # Phase-dispatched diagnosis.
    logs_needed: list[str] = []
    if phase == "Pending":
        sections.append("\n## Scheduling")
        sections.extend(["\n".join(_scheduling_lines(pod, core))])
    else:
        problems, logs_needed = _analyze_containers(pod)
        sections.append("\n## Diagnosis")
        if problems:
            sections.append("\n".join(problems))
        else:
            sections.append("✅ no container-level problems detected")

    # Previous-log tails for crashing containers.
    for cname in logs_needed:
        sections.append(f"\n## Previous logs — {cname} (last {_PREV_LOG_TAIL} lines)")
        sections.append(_previous_logs_section(name, namespace, cname))

    # Recent events — the scheduler / kubelet narrative.
    sections.append("\n## Recent events")
    sections.append(
        events_mod.get_events_for_object(
            "Pod", name, namespace, limit=_EVENTS_LIMIT,
        )
    )

    return "\n".join(sections)


def register(mcp) -> None:
    mcp.tool()(diagnose_pod)
