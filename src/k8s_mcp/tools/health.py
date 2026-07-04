"""Cluster-wide health snapshot — one tool, many sections.

`cluster_health_snapshot` is the entry-point for "how is the cluster right
now?" It's intentionally read-only and aggregates data from several
apiserver queries (plus a couple of local-file reads) so the agent
doesn't have to chain 8 tool calls just to triage.

What it covers (each section is independently error-bounded — one
failing section won't blank the whole report):

  1. Nodes — Ready vs NotReady, pressure conditions
  2. Resource Usage — top CPU/mem nodes + pods (metrics-server path;
     degrades to a one-liner install hint when metrics-server absent)
  3. Pending Pods + 4. Abnormal restarts (CrashLoopBackOff etc.)
  5. Pod Distribution — count by phase (Running/Pending/Failed/Succeeded)
  6. Image Pull Issues — pods stuck on ErrImagePull / ImagePullBackOff
     (a separately-actionable subset of "abnormal restarts")
  7. Workloads — counts of Deployments / StatefulSets / DaemonSets /
     ReplicaSets / Jobs / CronJobs
  8. HPA — current vs desired
  9. Orphan PVs (Released / Available with no claim)
 10. Certificates (delegates to certs.get_certificate_expiry)
 11. Recent Warning events (delegates to events.list_events)

What it deliberately does NOT cover (out of scope for an MVP snapshot):

  - Services without endpoints — too expensive (N API calls per Svc).
    If the user asks specifically, point them at `list_resources` +
    a follow-up `get_resource_yaml`.
  - Per-container resource pressure / OOMKills — surfaced under
    "Abnormal restarts" only when restartCount crosses the threshold.
  - Network policy coverage — too many false positives on dev clusters.
  - Per-node CPU/memory allocatable vs capacity — too noisy; the
    Resource Usage section already surfaces the top consumers.

中文说明：
本地 AI 运维的入口工具。Agent 被问「集群现在怎么样？」时一次调这个
就够。每节独立容错，某一节挂了不影响其它节，运维能继续看剩下
部分做判断。
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from ..client import get_api_client
from ..formatters import short_table
from . import certs, events, metrics

logger = logging.getLogger(__name__)


# Container-state reasons that mean the pod is stuck / thrashing, not
# "just restarting cleanly". These are the ones the operator should see.
_BAD_WAITING_REASONS = {
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "ErrImagePull",
    "CreateContainerConfigError",
    "InvalidImageName",
}


# ---------- low-level readers ------------------------------------------------


def _core_v1():
    from kubernetes import client
    return client.CoreV1Api(get_api_client())


def _autoscaling_v2():
    from kubernetes import client
    return client.AutoscalingV2Api(get_api_client())


def _apps_v1():
    from kubernetes import client
    return client.AppsV1Api(get_api_client())


def _batch_v1():
    from kubernetes import client
    return client.BatchV1Api(get_api_client())


def _list_pods(namespaces: list[str] | None) -> list[Any]:
    api = _core_v1()
    if namespaces:
        out = []
        for ns in namespaces:
            ret = api.list_namespaced_pod(ns)
            out.extend(ret.items)
        return out
    return list(api.list_pod_for_all_namespaces().items)


# ---------- section builders -------------------------------------------------


def _section_nodes() -> str:
    api = _core_v1()
    nodes = list(api.list_node().items)
    if not nodes:
        return "## Nodes\n(no nodes)"
    not_ready = []
    pressure = []
    for n in nodes:
        name = n.metadata.name
        status = n.status
        for cond in (status.conditions or []):
            if cond.type == "Ready" and cond.status != "True":
                not_ready.append(f"{name} (since {_rel_time(cond.last_transition_time)})")
            if cond.type in ("DiskPressure", "MemoryPressure", "PIDPressure") and cond.status == "True":
                pressure.append(f"{name}: {cond.type}")
    lines = [
        "## Nodes",
        f"Total: {len(nodes)}    Ready: {len(nodes) - len(not_ready)}/{len(nodes)}",
    ]
    if not_ready:
        lines.append("NotReady:")
        for n in not_ready:
            lines.append(f"  - {n}")
    else:
        lines.append("NotReady: (none)")
    if pressure:
        lines.append("Pressure:")
        for p in pressure:
            lines.append(f"  - {p}")
    else:
        lines.append("Pressure: (none)")
    return "\n".join(lines)


def _section_pending_pods(namespaces: list[str] | None) -> str:
    pods = [p for p in _list_pods(namespaces) if p.status.phase == "Pending"]
    if not pods:
        return "## Pending Pods\n(none — all scheduled)"
    rows = []
    for p in pods[:20]:  # cap to avoid flooding
        # Find first non-trivial reason from conditions
        reason = "unknown"
        for cond in (p.status.conditions or []):
            if cond.reason and cond.reason != "PodScheduled":
                reason = cond.reason
                break
        rows.append({
            "POD": f"{p.metadata.namespace}/{p.metadata.name}",
            "REASON": reason,
            "MESSAGE": (cond.message if (cond := (p.status.conditions or [None])[-1]) else "")[:80],
        })
    extra = f"\n(showing first {len(rows)} of {len(pods)})" if len(pods) > 20 else ""
    return f"## Pending Pods ({len(pods)})\n" + short_table(rows, ["POD", "REASON", "MESSAGE"]) + extra


def _section_abnormal_restarts(namespaces: list[str] | None, threshold: int) -> str:
    """Pods with restartCount > threshold OR in a bad waiting state."""
    rows: list[dict] = []
    for p in _list_pods(namespaces):
        for cs in (p.status.container_statuses or []):
            waiting = (cs.state.waiting.reason if cs.state.waiting else None)
            if cs.restart_count > threshold or waiting in _BAD_WAITING_REASONS:
                rows.append({
                    "POD": f"{p.metadata.namespace}/{p.metadata.name}",
                    "CONTAINER": cs.name,
                    "RESTARTS": str(cs.restart_count),
                    "STATE": waiting or ("Running" if cs.state.running else "?"),
                })
    if not rows:
        return f"## Abnormal Restarts\n(none — threshold: {threshold} restarts)"
    rows.sort(key=lambda r: int(r["RESTARTS"]), reverse=True)
    return f"## Abnormal Restarts ({len(rows)} — threshold: {threshold} restarts)\n" + \
        short_table(rows[:20], ["POD", "CONTAINER", "RESTARTS", "STATE"])


def _section_hpa(namespaces: list[str] | None) -> str:
    api = _autoscaling_v2()
    if namespaces:
        hpas = []
        for ns in namespaces:
            hpas.extend(api.list_namespaced_horizontal_pod_autoscaler(ns).items)
    else:
        hpas = list(api.list_horizontal_pod_autoscaler_for_all_namespaces().items)
    rows = []
    for h in hpas:
        status = h.status
        spec = h.spec
        cur = status.current_replicas or 0
        des = status.desired_replicas or 0
        if cur == des:
            continue  # only show actionable rows
        mn = spec.min_replicas or 0
        mx = spec.max_replicas
        note = ""
        if des > mx:
            note = f"⚠️ desired {des} > max {mx}"
        elif cur == 0 and des == 0:
            note = "scaled to zero"
        elif cur < des:
            note = f"scaling up {cur}→{des}"
        else:
            note = f"scaling down {cur}→{des}"
        rows.append({
            "HPA": f"{h.metadata.namespace}/{h.metadata.name}",
            "TARGET": f"{spec.scale_target_ref.kind}/{spec.scale_target_ref.name}",
            "CUR/DES/MIN-MAX": f"{cur}/{des}/{mn}-{mx}",
            "NOTE": note,
        })
    if not rows:
        return "## HPA\n(all HPAs at desired replicas)"
    return f"## HPA ({len(rows)} not at desired)\n" + \
        short_table(rows, ["HPA", "TARGET", "CUR/DES/MIN-MAX", "NOTE"])


def _section_orphan_pvs() -> str:
    """Released (was bound, claim deleted) — actionable cleanup.
    Available with no claimRef — operator may have created it
    intentionally; we still flag it.
    Failed — definitely a problem.
    """
    api = _core_v1()
    pvs = list(api.list_persistent_volume().items)
    rows = []
    for pv in pvs:
        phase = pv.status.phase
        if phase == "Bound":
            continue
        claim = pv.spec.claim_ref
        claim_str = f"{claim.namespace}/{claim.name}" if claim and claim.name else "—"
        rows.append({
            "PV": pv.metadata.name,
            "PHASE": phase,
            "CLAIM_REF": claim_str,
            "AGE": _rel_time(pv.metadata.creation_timestamp),
        })
    if not rows:
        return "## Orphan PVs\n(all PVs Bound)"
    rows.sort(key=lambda r: 0 if r["PHASE"] == "Failed" else 1 if r["PHASE"] == "Released" else 2)
    return f"## Orphan PVs ({len(rows)})\n" + short_table(rows, ["PV", "PHASE", "CLAIM_REF", "AGE"])


def _section_certificates() -> str:
    # Delegate to the existing certs tool — keeps the parsing logic
    # in one place.
    return "## Certificates\n" + certs.get_certificate_expiry()


def _section_recent_warnings(minutes: int, namespaces: list[str] | None) -> str:
    from kubernetes.client.rest import ApiException
    try:
        # We can't pass arbitrary time filters to list_events (it doesn't
        # take a "since" arg), but listing top-N warning events sorted by
        # lastTimestamp descending and truncating client-side is good
        # enough for an MVP snapshot.
        ns = namespaces[0] if namespaces and len(namespaces) == 1 else None
        out = events.list_events(
            namespace=ns,
            warning_only=True,
            limit=20,
        )
    except ApiException as e:
        return f"## Recent Warning Events\n(events API failed: {e.reason})"
    if not out.strip() or out.strip() == "(no events)":
        return f"## Recent Warning Events (last ~{minutes}m)\n(none)"
    return f"## Recent Warning Events (top {20} by last-seen)\n{out}"


def _section_resource_usage(namespaces: list[str] | None, top_n: int = 5) -> str:
    """Top CPU/memory consumers among Nodes + Pods (metrics-server).

    Falls back to a one-liner with the metrics-server install hint when
    metrics-server isn't deployed — common on dev clusters. Pod query
    scope honors `namespaces`; nodes are always cluster-wide.
    """
    out: list[str] = ["## Resource Usage"]

    # --- Nodes (always cluster-wide) ---
    try:
        node_table = metrics.top_nodes()
        rows = _parse_short_table(node_table)
        if not rows:
            out.append("### Top Nodes\n(no node metrics)")
        else:
            head = rows[0:2]  # header + separator
            body = rows[2:2 + top_n]
            out.append("### Top Nodes (memory desc)")
            out.append("\n".join(head + body))
            if len(rows) - 2 > top_n:
                out.append(f"(showing top {top_n} of {len(rows) - 2} nodes)")
    except RuntimeError as e:
        if "metrics-server" in str(e):
            out.append(
                "(metrics-server not installed — install with:\n"
                "  kubectl apply -f https://github.com/kubernetes-sigs/"
                "metrics-server/releases/latest/download/components.yaml)"
            )
            return "\n".join(out)
        raise

    # --- Pods (scope = namespaces if exactly one given, else cluster-wide) ---
    out.append("")
    pod_ns = namespaces[0] if namespaces and len(namespaces) == 1 else None
    try:
        pod_table = metrics.top_pods(namespace=pod_ns)
        rows = _parse_short_table(pod_table)
        if not rows:
            out.append(
                f"### Top Pods{' in ' + pod_ns if pod_ns else ''}\n(no pod metrics)"
            )
        else:
            head = rows[0:2]
            body = rows[2:2 + top_n]
            scope = f" in {pod_ns}" if pod_ns else ""
            out.append(f"### Top Pods{scope} (memory desc)")
            out.append("\n".join(head + body))
            if len(rows) - 2 > top_n:
                out.append(f"(showing top {top_n} of {len(rows) - 2} pods)")
    except RuntimeError as e:
        if "metrics-server" in str(e):
            out.append("### Top Pods\n(metrics-server not installed — skipping)")
            return "\n".join(out)
        raise
    return "\n".join(out)


def _section_pod_distribution(namespaces: list[str] | None) -> str:
    """Count of pods per phase (Running / Pending / Failed / Succeeded / Unknown).

    Cheaper to render than the full `list_pods` table but gives the
    operator a "what's the cluster actually doing" one-liner.
    """
    counts: dict[str, int] = {
        "Running": 0, "Pending": 0, "Failed": 0,
        "Succeeded": 0, "Unknown": 0,
    }
    for p in _list_pods(namespaces):
        phase = p.status.phase or "Unknown"
        counts[phase if phase in counts else "Unknown"] += 1
    total = sum(counts.values())
    rows = [
        {"PHASE": k, "COUNT": str(v)}
        for k, v in sorted(counts.items(), key=lambda kv: -kv[1]) if v > 0
    ]
    if not rows:
        return "## Pod Distribution\n(no pods)"
    return f"## Pod Distribution (total {total})\n" + \
        short_table(rows, ["PHASE", "COUNT"])


def _section_image_pull(namespaces: list[str] | None) -> str:
    """Pods stuck on image-pull failures (distinct actionable signal).

    Separate from "Abnormal Restarts" because image-pull pods typically
    have `restartCount=0` (the container never ran) and a `waiting.reason`
    of `ErrImagePull` / `ImagePullBackOff` / `InvalidImageName`. The fix
    is usually edit the image reference or check `imagePullSecrets` /
    registry creds — different from the restart-thrash category.
    """
    rows: list[dict] = []
    for p in _list_pods(namespaces):
        for cs in (p.status.container_statuses or []):
            waiting = (cs.state.waiting.reason if cs.state.waiting else None)
            if waiting in ("ErrImagePull", "ImagePullBackOff", "InvalidImageName"):
                rows.append({
                    "POD": f"{p.metadata.namespace}/{p.metadata.name}",
                    "CONTAINER": cs.name,
                    "REASON": waiting,
                    "MESSAGE": (cs.state.waiting.message or "")[:100],
                })
    if not rows:
        return "## Image Pull Issues\n(none — all images resolved)"
    rows.sort(key=lambda r: (r["REASON"], r["POD"]))
    return f"## Image Pull Issues ({len(rows)})\n" + \
        short_table(rows[:20], ["POD", "CONTAINER", "REASON", "MESSAGE"])


def _section_workloads(namespaces: list[str] | None) -> str:
    """Count of Deployments / StatefulSets / DaemonSets / ReplicaSets
    / Jobs / CronJobs — a "how many workloads am I responsible for"
    one-glance summary.

    Each kind's list call is wrapped independently so one bad call
    (e.g. RBAC denied on CronJobs) shows as `?` rather than blanking
    the section.
    """
    apps = _apps_v1()
    batch = _batch_v1()
    nss = namespaces

    def _count(ns_fn, all_fn):
        try:
            if nss:
                return sum(len(ns_fn(ns).items) for ns in nss)
            return len(all_fn().items)
        except Exception:  # noqa: BLE001
            return "?"

    pairs = [
        ("Deployment",  apps.list_namespaced_deployment,
                       apps.list_deployment_for_all_namespaces),
        ("StatefulSet", apps.list_namespaced_stateful_set,
                       apps.list_stateful_set_for_all_namespaces),
        ("DaemonSet",   apps.list_namespaced_daemon_set,
                       apps.list_daemon_set_for_all_namespaces),
        ("ReplicaSet",  apps.list_namespaced_replica_set,
                       apps.list_replica_set_for_all_namespaces),
        ("Job",         batch.list_namespaced_job,
                       batch.list_job_for_all_namespaces),
        ("CronJob",     batch.list_namespaced_cron_job,
                       batch.list_cron_job_for_all_namespaces),
    ]
    rows = [
        {"KIND": label, "COUNT": str(_count(ns_fn, all_fn))}
        for label, ns_fn, all_fn in pairs
    ]
    return "## Workloads\n" + short_table(rows, ["KIND", "COUNT"])


def _parse_short_table(table_str: str) -> list[str]:
    """Split a `short_table` string into lines, dropping any blanks.

    Used by Resource Usage to truncate to top-N while preserving the
    header + separator + first N body rows.
    """
    return [ln for ln in table_str.splitlines() if ln.strip()]


# ---------- headline summary -------------------------------------------------


def _headline(
    node_total: int, not_ready: int, pending: int,
    abnormal: int, image_pull: int, expiring_certs: int,
    hpa_off: int, orphan_pvs: int,
) -> str:
    """Top-of-report one-liner. Color-coded plain text — no ANSI."""
    if (not_ready + pending + abnormal + image_pull
            + expiring_certs + hpa_off + orphan_pvs) == 0:
        overall = "✅ HEALTHY"
    else:
        overall = "⚠️  ATTENTION"
    bits = [
        f"Nodes: {node_total - not_ready}/{node_total} Ready",
        f"Pending Pods: {pending}",
        f"Abnormal Restarts: {abnormal}",
        f"Image Pull: {image_pull}",
        f"HPA off-target: {hpa_off}",
        f"Orphan PVs: {orphan_pvs}",
        f"Certs expiring: {expiring_certs}",
    ]
    return f"## {overall}  ({', '.join(bits)})"


# ---------- entry point ------------------------------------------------------


def cluster_health_snapshot(
    namespaces: list[str] | None = None,
    events_minutes: int = 60,
    restart_threshold: int = 3,
) -> str:
    """Return a multi-section cluster health report in one call.

    This is the entry-point tool for "how is the cluster right now?"
    Itself does NOT make any cluster modifications — safe to call from
    any namespace, even in read-only mode.

    Note: prefer reusing the most recent result for the same query rather
    than re-calling if the underlying state is unlikely to have changed. New
    calls remain valid when verifying a mutation's effect.

    Args:
        namespaces: limit pod/HPA/event sections to these namespaces.
            Nodes, certificates, and orphan-PV are always cluster-wide.
            None (default) = all namespaces.
        events_minutes: hint in the recent-events header. The events
            API doesn't accept a time filter; we list top-20 warnings
            sorted by lastTimestamp desc.
        restart_threshold: a pod is "abnormal" if any container's
            restartCount exceeds this (default 3) OR if it's in
            CrashLoopBackOff / ImagePullBackOff / ErrImagePull /
            CreateContainerConfigError. Set higher (e.g. 10) on busy
            clusters where restarts are routine.

    Each section is independently error-bounded — a failing section
    renders as `## <name>\n(error: <reason>)` and the rest of the
    report still ships. The headline is a one-line aggregate.

    Typical use:
      - "Is the cluster healthy?" → call this, read the headline.
      - "Why is X failing?" → call this, then drill into the relevant
        section's specific names with `describe_resource` /
        `get_resource_yaml` / `get_pod_logs`.
    """
    sections: list[str] = []
    headline_counts = {
        "node_total": 0, "not_ready": 0, "pending": 0,
        "abnormal": 0, "image_pull": 0, "expiring_certs": 0,
        "hpa_off": 0, "orphan_pvs": 0,
    }

    section_builders = [
        ("Nodes", _section_nodes, lambda s: _count_from_section(s, "NotReady:") and "  "),
        ("Resource Usage", lambda: _section_resource_usage(namespaces),
         lambda s: 0),  # informational; no headline contribution
        ("Pending Pods", lambda: _section_pending_pods(namespaces),
         lambda s: _int_after(s, "## Pending Pods (")),
        ("Abnormal Restarts",
         lambda: _section_abnormal_restarts(namespaces, restart_threshold),
         lambda s: _int_after(s, "## Abnormal Restarts (")),
        ("Pod Distribution", lambda: _section_pod_distribution(namespaces),
         lambda s: 0),  # informational
        ("Image Pull Issues", lambda: _section_image_pull(namespaces),
         lambda s: _int_after(s, "## Image Pull Issues (")
         if "all images resolved" not in s else 0),
        ("Workloads", lambda: _section_workloads(namespaces),
         lambda s: 0),  # informational
        ("HPA", lambda: _section_hpa(namespaces),
         lambda s: _int_after(s, "## HPA (") if "not at desired" in s else 0),
        ("Orphan PVs", _section_orphan_pvs,
         lambda s: _int_after(s, "## Orphan PVs (") if "Bound" not in s else 0),
        ("Certificates", _section_certificates,
         _count_expiring_certs),
        ("Recent Warning Events",
         lambda: _section_recent_warnings(events_minutes, namespaces),
         lambda s: 0),  # events are advisory; don't count toward headline
    ]

    for name, builder, count_fn in section_builders:
        try:
            body = builder()
        except Exception as e:  # noqa: BLE001
            logger.warning("health snapshot section %s failed: %s", name, e)
            sections.append(f"## {name}\n(section failed: {type(e).__name__}: {e})")
            continue
        sections.append(body)
        try:
            c = count_fn(body)
            if name == "Nodes":
                headline_counts["node_total"], headline_counts["not_ready"] = _node_counts(body)
            elif name == "Pending Pods":
                headline_counts["pending"] = c
            elif name == "Abnormal Restarts":
                headline_counts["abnormal"] = c
            elif name == "Image Pull Issues":
                headline_counts["image_pull"] = c
            elif name == "HPA":
                headline_counts["hpa_off"] = c
            elif name == "Orphan PVs":
                headline_counts["orphan_pvs"] = c
            elif name == "Certificates":
                headline_counts["expiring_certs"] = c
        except Exception:  # noqa: BLE001
            pass

    header = (
        f"# Cluster Health Snapshot — "
        f"{datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )
    headline = _headline(**headline_counts)
    return header + "\n\n" + headline + "\n\n" + "\n\n".join(sections) + "\n"


# ---------- tiny parsers used by the headline ------------------------------


def _count_from_section(s: str, marker: str) -> bool:
    """Placeholder for future use; not all sections need a boolean."""
    return marker in s


def _int_after(s: str, marker: str) -> int:
    """Parse an integer that appears immediately after `marker` in `s`.

    Example: marker="## HPA (",  s="## HPA (3 not at desired)\n..."  → 3
    """
    idx = s.find(marker)
    if idx < 0:
        return 0
    tail = s[idx + len(marker):]
    digits = ""
    for ch in tail:
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    return int(digits) if digits else 0


def _node_counts(nodes_section: str) -> tuple[int, int]:
    """Extract (total, not_ready) from the Nodes section text.

    Section format we generate:
        ## Nodes
        Total: 9    Ready: 8/9
        NotReady:
          - deploy-2 (since 5m ago)
    """
    import re
    total_m = re.search(r"Total:\s*(\d+)", nodes_section)
    not_ready_m = re.search(r"NotReady:\s*\n((?:\s*-\s*.+\n?)+)", nodes_section)
    total = int(total_m.group(1)) if total_m else 0
    not_ready = 0
    if not_ready_m:
        not_ready = len(re.findall(r"^\s*-\s", not_ready_m.group(1), re.MULTILINE))
    return total, not_ready


def _count_expiring_certs(certs_section: str) -> int:
    """Count rows in the certificate table whose STATUS is not 'valid'."""
    return sum(
        1 for line in certs_section.splitlines()
        if line and not line.startswith(("SOURCE", "##", "Action", "  -", "Note:", "  "))
        and ("<30d" in line or "<7d" in line or "EXPIRED" in line)
    )


# ---------- time helper ------------------------------------------------------


def _rel_time(ts) -> str:
    if not ts:
        return "?"
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    secs = int((datetime.now(UTC) - ts).total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def register(mcp) -> None:
    mcp.tool()(cluster_health_snapshot)
