"""Cluster-wide health snapshot — one tool, many sections.

`cluster_health_snapshot` is the entry-point for "how is the cluster right
now?" It's intentionally read-only and aggregates data from several
apiserver queries (plus a couple of local-file reads) so the agent
doesn't have to chain 8 tool calls just to triage.

What it covers (each section is independently error-bounded — one
failing section won't blank the whole report):

  1. Nodes — Ready vs NotReady, pressure conditions
  2. Pending Pods + 3. Abnormal restarts (CrashLoopBackOff etc.)
  4. HPA — current vs desired
  5. Orphan PVs (Released / Available with no claim)
  6. Certificates (delegates to certs.get_certificate_expiry)
  7. Recent Warning events (delegates to events.list_events)

What it deliberately does NOT cover (out of scope for an MVP snapshot):

  - Services without endpoints — too expensive (N API calls per Svc).
    If the user asks specifically, point them at `list_resources` +
    a follow-up `get_resource_yaml`.
  - Per-container resource pressure / OOMKills — surfaced under
    "Abnormal restarts" only when restartCount crosses the threshold.
  - Network policy coverage — too many false positives on dev clusters.

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
from . import certs, events

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


# ---------- headline summary -------------------------------------------------


def _headline(
    node_total: int, not_ready: int, pending: int,
    abnormal: int, expiring_certs: int, hpa_off: int, orphan_pvs: int,
) -> str:
    """Top-of-report one-liner. Color-coded plain text — no ANSI."""
    if (not_ready + pending + abnormal + expiring_certs + hpa_off + orphan_pvs) == 0:
        overall = "✅ HEALTHY"
    else:
        overall = "⚠️  ATTENTION"
    bits = [
        f"Nodes: {node_total - not_ready}/{node_total} Ready",
        f"Pending Pods: {pending}",
        f"Abnormal Restarts: {abnormal}",
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
        "abnormal": 0, "expiring_certs": 0, "hpa_off": 0, "orphan_pvs": 0,
    }

    section_builders = [
        ("Nodes", _section_nodes, lambda s: _count_from_section(s, "NotReady:") and "  "),
        ("Pending Pods", lambda: _section_pending_pods(namespaces),
         lambda s: _int_after(s, "## Pending Pods (")),
        ("Abnormal Restarts",
         lambda: _section_abnormal_restarts(namespaces, restart_threshold),
         lambda s: _int_after(s, "## Abnormal Restarts (")),
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
