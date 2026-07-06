"""Pod / Deployment diagnostics — one-shot triage aggregators.

中文说明：
- `diagnose_pod`：拿一个 Pod 名，按 phase 自动分派出一份分层报告。
  Pending 走调度诊断（突出调度器自己的 Unschedulable 裁决 + PVC 绑定 +
  requests 汇总，不重算每节点拟合——那是重复调度器且易错），Running /
  CrashLoop 走运行时诊断（容器 state/lastState、OOMKilled、exit code、
  restart，CrashLoop 时自动 tail previous 容器日志）。
- `diagnose_deployment`：拿一个 Deployment 名，输出一份排障报告：replicas
  desired/ready/updated/available、Strategy、owned ReplicaSets（old 缩
  zero + new 拉伸中、或 new 已就绪），new RS 的 Pod 阶段分布 + 引用
  `diagnose_pod` 进一步下钻，最近 events。
  全只读。
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


def _apps_v1():
    return client.AppsV1Api(get_api_client())


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


# ---------------------------------------------------------------------------
# Deployment-level diagnosis
# ---------------------------------------------------------------------------


def _owned_replica_sets(deployment, apps, namespace: str) -> list:
    """Find the ReplicaSets owned by this Deployment.

    Strategy: K8s tags every RS created by a Deployment with
    `pod-template-hash`; the deployment name itself is NOT a default label
    on the RS, but the RS's `ownerReferences` points at the Deployment.
    We use a label-selector of `spec.selector.matchLabels` (which the
    controller-manager copies verbatim onto the RS) — that avoids walking
    every RS in the namespace to filter by ownerRef.
    """
    match = (deployment.spec.selector.match_labels or {}) if deployment.spec.selector else {}
    if not match:
        return []
    label_selector = ",".join(f"{k}={v}" for k, v in match.items())
    ret = apps.list_namespaced_replica_set(namespace, label_selector=label_selector)
    return list(ret.items)


def _rs_image(rs) -> str:
    """Return the image of the first container in the RS pod template."""
    containers = (rs.spec.template.spec.containers or []) if rs.spec and rs.spec.template else []
    if not containers:
        return ""
    return containers[0].image or ""


def _new_replica_set(replicasets) -> tuple | None:
    """Pick the active (newest) RS — the one with the highest pod-template-hash.

    Returns (rs, is_old) where is_old=True means the RS is being scaled
    down (current_replicas == 0). Returns None if there is no RS at all.
    """
    active = None
    for rs in replicasets:
        # K8s always injects `pod-template-hash` into the pod-template labels.
        labels = (rs.spec.template.metadata.labels or {}) if rs.spec and rs.spec.template else {}
        h = labels.get("pod-template-hash") or ""
        if active is None or h > (active[1] or ""):
            active = (rs, h)
    if active is None:
        return None
    return active


def _rollout_summary(deployment) -> str:
    """One-line: desired vs ready vs updated vs available + Progressing condition."""
    s = deployment.status
    desired = (deployment.spec.replicas if deployment.spec.replicas is not None else 0)
    ready = s.ready_replicas or 0
    updated = s.updated_replicas or 0
    available = s.available_replicas or 0
    parts = [
        f"desired={desired}",
        f"ready={ready}",
        f"updated={updated}",
        f"available={available}",
    ]
    line = "  " + " | ".join(parts)
    progressing = next(
        (c for c in (s.conditions or []) if c.type == "Progressing"), None
    )
    if progressing is not None:
        marker = "✅" if progressing.status == "True" else "❌"
        reason = progressing.reason or "?"
        line += f"\n  Progressing: {marker} {reason} — {progressing.message or ''}"
    return line


def diagnose_deployment(name: str, namespace: str = "default") -> str:
    """🔍 DIAGNOSE DEPLOYMENT — one-shot triage for a single Deployment.

    Aggregates the ~5 calls an agent otherwise makes serially (get
    deployment, list owned RS, list pods owned by the new RS, tail their
    phases, read events) into a single layered report:

    - **Rollout** — `desired/ready/updated/available` summary + the
      `Progressing` condition's reason & message (the controller's own
      verdict — e.g. `NewReplicaSetAvailable` or `ProgressDeadlineExceeded`).
    - **ReplicaSets** — owned RS list, one row each: template-hash,
      desired/current/ready replicas, and the first container's image
      (so an old vs new image diff is obvious from a single table).
    - **New ReplicaSet** — pod phase distribution (`Running / Pending /
      CrashLoop / Failed`). If any pod is in `Pending` or
      `CrashLoopBackOff`, the report ends with a literal "next step:
      call `diagnose_pod(name=<pod>, namespace=<ns>)`" so the agent
      doesn't have to guess.
    - **Recent events** — the deployment + new RS event stream.

    Read-only — no mutations, safe to call repeatedly.

    Args:
        name: Deployment name.
        namespace: target namespace (default "default").

    Returns a multi-section markdown report. Raises ValueError if the
    Deployment does not exist.
    """
    apps = _apps_v1()
    core = _core_v1()
    try:
        deployment = apps.read_namespaced_deployment(name, namespace)
    except ApiException as e:
        if e.status == 404:
            raise ValueError(
                f"Deployment {namespace}/{name} not found"
            ) from e
        raise RuntimeError(
            f"failed to read Deployment {namespace}/{name}: "
            f"{e.status} {e.reason}"
        ) from e

    age = format_age(deployment.metadata.creation_timestamp)
    strategy = (deployment.spec.strategy.type or "RollingUpdate") if deployment.spec.strategy else "?"
    selector = (deployment.spec.selector.match_labels or {}) if deployment.spec.selector else {}

    sections: list[str] = [
        f"## Deployment {namespace}/{name}",
        f"Strategy: {strategy} | Age: {age} | Selector: {selector or '(empty)'}",
    ]

    # Rollout summary — this is the cheapest "is it done?" signal.
    sections.append("\n## Rollout")
    sections.append(_rollout_summary(deployment))

    # Owned ReplicaSets — the meat of "which version is running".
    replicasets = _owned_replica_sets(deployment, apps, namespace)
    if replicasets:
        rs_rows = []
        for rs in replicasets:
            labels = (
                (rs.spec.template.metadata.labels or {})
                if rs.spec and rs.spec.template else {}
            )
            hash_label = labels.get("pod-template-hash") or "?"
            rs_rows.append({
                "REPLICASET": rs.metadata.name,
                "HASH": hash_label[:8],
                "DESIRED": str(rs.spec.replicas or 0),
                "CURRENT": str(rs.status.replicas or 0),
                "READY": str(rs.status.ready_replicas or 0),
                "IMAGE": _rs_image(rs),
            })
        # Newest first.
        rs_rows.sort(key=lambda r: r["HASH"], reverse=True)
        sections.append("\n## ReplicaSets")
        sections.append(short_table(
            rs_rows,
            ["REPLICASET", "HASH", "DESIRED", "CURRENT", "READY", "IMAGE"],
        ))
    else:
        sections.append("\n## ReplicaSets")
        sections.append("(no ReplicaSets found for selector — controller not yet reconciled?)")

    # New ReplicaSet + its pods — the "what is the rollout actually doing
    # right now" view.
    active = _new_replica_set(replicasets) if replicasets else None
    if active is not None:
        new_rs, _hash = active
        new_rs_name = new_rs.metadata.name
        new_rs_image = _rs_image(new_rs)
        new_rs_desired = new_rs.spec.replicas or 0
        new_rs_ready = new_rs.status.ready_replicas or 0
        sections.append("\n## New ReplicaSet")
        sections.append(
            f"{new_rs_name} — image: {new_rs_image or '(none)'} — "
            f"ready {new_rs_ready}/{new_rs_desired}"
        )

        # List the pods owned by THIS rs only.
        pod_rows: list[dict] = []
        try:
            pod_list = core.list_namespaced_pod(
                namespace,
                label_selector=(
                    f"pod-template-hash={_hash}" if _hash else None
                ),
            )
        except ApiException as e:
            pod_list = None
            sections.append(f"  (failed to list pods: {e.status} {e.reason})")

        if pod_list is not None and pod_list.items:
            for p in pod_list.items:
                phase = (p.status.phase or "?") if p.status else "?"
                restarts = sum(
                    cs.restart_count for cs in (p.status.container_statuses or [])
                )
                pod_rows.append({
                    "POD": p.metadata.name,
                    "PHASE": phase,
                    "RESTARTS": str(restarts),
                    "NODE": p.spec.node_name or "",
                    "AGE": format_age(p.metadata.creation_timestamp),
                })
            sections.append(short_table(
                pod_rows,
                ["POD", "PHASE", "RESTARTS", "NODE", "AGE"],
            ))

            problem_pods = [
                p for p in pod_list.items
                if (p.status.phase or "") in ("Pending", "Failed")
                or any(
                    (cs.state.waiting and cs.state.waiting.reason == "CrashLoopBackOff")
                    for cs in (p.status.container_statuses or [])
                )
            ]
            if problem_pods:
                first = problem_pods[0]
                sections.append(
                    f"\n⚠️ {len(problem_pods)}/{len(pod_list.items)} pod(s) "
                    f"need attention. Next step: call "
                    f"`diagnose_pod(name={first.metadata.name!r}, "
                    f"namespace={namespace!r})` for the first one."
                )

    # Recent events — the controller + scheduler narrative.
    sections.append("\n## Recent events")
    sections.append(
        events_mod.get_events_for_object(
            "Deployment", name, namespace, limit=_EVENTS_LIMIT,
        )
    )

    return "\n".join(sections)


def register(mcp) -> None:
    mcp.tool()(diagnose_pod)
    mcp.tool()(diagnose_deployment)
