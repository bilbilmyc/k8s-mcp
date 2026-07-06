"""Tests for `diagnose_pod` — one-shot Pod triage.

We stub `diagnostics._core_v1` with a fake CoreV1 and patch the two
reused helpers (`events_mod.get_events_for_object`, `logs_mod.get_pod_logs`)
so nothing touches an apiserver. Pods are built from SimpleNamespace to
mirror the attribute shape of the kubernetes client models.
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace as Ns
from unittest.mock import patch

import pytest
from kubernetes.client.rest import ApiException

from k8s_mcp.tools import diagnostics

# ---------- builders --------------------------------------------------------


def _state(running=False, waiting=None, terminated=None):
    return Ns(
        running=Ns() if running else None,
        waiting=waiting,
        terminated=terminated,
    )


def _waiting(reason, message=None):
    return Ns(reason=reason, message=message)


def _terminated(reason, exit_code):
    return Ns(reason=reason, exit_code=exit_code)


def _cs(name, *, ready=True, restart_count=0, state=None, last_terminated=None):
    return Ns(
        name=name,
        ready=ready,
        restart_count=restart_count,
        state=state or _state(running=True),
        last_state=Ns(terminated=last_terminated),
    )


def _spec_container(name="app", cpu=None, memory=None):
    req: dict = {}
    if cpu:
        req["cpu"] = cpu
    if memory:
        req["memory"] = memory
    return Ns(name=name, resources=Ns(requests=req or None))


def _pvc_volume(claim_name):
    return Ns(persistent_volume_claim=Ns(claim_name=claim_name))


def _pod(
    *,
    phase="Running",
    node="node-1",
    containers=None,
    init=None,
    volumes=None,
    conditions=None,
    spec_containers=None,
):
    meta = Ns(
        name="p",
        namespace="default",
        creation_timestamp=datetime(2026, 7, 6, tzinfo=UTC),
    )
    status = Ns(
        phase=phase,
        container_statuses=containers or [],
        init_container_statuses=init or [],
        conditions=conditions or [],
    )
    spec = Ns(
        node_name=node,
        volumes=volumes or [],
        containers=spec_containers or [],
    )
    return Ns(metadata=meta, status=status, spec=spec)


class _FakeCore:
    def __init__(self, pod=None, *, read_raises=None, pvc_phase="Bound"):
        self._pod = pod
        self._read_raises = read_raises
        self._pvc_phase = pvc_phase

    def read_namespaced_pod(self, name, namespace):
        if self._read_raises is not None:
            raise self._read_raises
        return self._pod

    def read_namespaced_persistent_volume_claim(self, name, namespace):
        return Ns(status=Ns(phase=self._pvc_phase))


def _run(pod=None, *, core=None, logs="line1\nfatal\n"):
    """Invoke diagnose_pod with all external deps stubbed.

    Returns (output, log_calls) where log_calls records get_pod_logs kwargs.
    """
    core = core or _FakeCore(pod)
    log_calls: list[dict] = []

    def fake_logs(**kwargs):
        log_calls.append(kwargs)
        return logs

    with patch.object(diagnostics, "_core_v1", return_value=core), \
            patch.object(
                diagnostics.events_mod, "get_events_for_object",
                return_value="(no events for Pod/p in namespace default)",
            ), \
            patch.object(diagnostics.logs_mod, "get_pod_logs", side_effect=fake_logs):
        out = diagnostics.diagnose_pod("p", "default")
    return out, log_calls


# ---------- lookup errors ---------------------------------------------------


def test_pod_not_found_raises_value_error():
    core = _FakeCore(read_raises=ApiException(status=404, reason="not found"))
    with pytest.raises(ValueError, match="not found"):
        _run(core=core)


def test_pod_read_other_error_raises_runtime():
    core = _FakeCore(read_raises=ApiException(status=403, reason="forbidden"))
    with pytest.raises(RuntimeError, match="403"):
        _run(core=core)


# ---------- healthy running -------------------------------------------------


def test_running_healthy_pod_reports_no_problems():
    pod = _pod(containers=[_cs("app", ready=True)])
    out, log_calls = _run(pod)
    assert "Phase: Running" in out
    assert "no container-level problems detected" in out
    assert log_calls == []  # no crashing container → no previous-log tail


def test_container_table_marks_init_containers():
    pod = _pod(
        init=[_cs("setup", state=_state(terminated=_terminated("Completed", 0)))],
        containers=[_cs("app")],
    )
    out, _ = _run(pod)
    assert "(init) setup" in out
    assert "## Containers" in out


# ---------- crash loop ------------------------------------------------------


def test_crashloop_reports_problem_and_tails_previous_logs():
    cs = _cs(
        "app",
        ready=False,
        restart_count=7,
        state=_state(waiting=_waiting("CrashLoopBackOff", "back-off restarting")),
        last_terminated=_terminated("Error", 1),
    )
    pod = _pod(containers=[cs])
    out, log_calls = _run(pod)

    assert "CrashLoopBackOff" in out
    assert "restarted 7×" in out
    assert "last exit 1" in out
    assert "## Previous logs — app" in out
    assert "fatal" in out
    # previous logs pulled exactly once for the crashing container
    assert len(log_calls) == 1
    assert log_calls[0]["previous"] is True
    assert log_calls[0]["container"] == "app"


def test_oomkilled_surfaces_memory_hint():
    cs = _cs(
        "app",
        restart_count=3,
        state=_state(running=True),
        last_terminated=_terminated("OOMKilled", 137),
    )
    pod = _pod(containers=[cs])
    out, _ = _run(pod)
    assert "OOMKilled" in out
    assert "OOM" in out
    assert "memory" in out.lower()


def test_nonzero_terminated_exit_flagged_and_tailed():
    cs = _cs(
        "app",
        ready=False,
        state=_state(terminated=_terminated("Error", 2)),
    )
    pod = _pod(phase="Failed", containers=[cs])
    out, log_calls = _run(pod)
    assert "terminated: Error (exit 2)" in out
    assert len(log_calls) == 1


def test_imagepullbackoff_surfaced_without_log_tail():
    cs = _cs(
        "app",
        ready=False,
        state=_state(waiting=_waiting("ImagePullBackOff", "pull access denied")),
    )
    pod = _pod(containers=[cs])
    out, log_calls = _run(pod)
    assert "ImagePullBackOff" in out
    assert "pull access denied" in out
    # never started → no previous logs to pull
    assert log_calls == []
    assert "## Previous logs" not in out


def test_previous_logs_failure_is_soft():
    cs = _cs(
        "app",
        state=_state(waiting=_waiting("CrashLoopBackOff")),
        last_terminated=_terminated("Error", 1),
    )
    pod = _pod(containers=[cs])

    def boom(**kwargs):
        raise RuntimeError("no previous terminated container")

    core = _FakeCore(pod)
    with patch.object(diagnostics, "_core_v1", return_value=core), \
            patch.object(
                diagnostics.events_mod, "get_events_for_object",
                return_value="(no events)",
            ), \
            patch.object(diagnostics.logs_mod, "get_pod_logs", side_effect=boom):
        out = diagnostics.diagnose_pod("p", "default")
    assert "previous logs unavailable" in out


# ---------- pending / scheduling --------------------------------------------


def test_pending_surfaces_scheduler_verdict():
    cond = Ns(
        type="PodScheduled",
        status="False",
        reason="Unschedulable",
        message="0/3 nodes are available: 3 Insufficient cpu.",
    )
    pod = _pod(
        phase="Pending",
        node=None,
        conditions=[cond],
        spec_containers=[_spec_container(cpu="2", memory="4Gi")],
    )
    out, _ = _run(pod)
    assert "## Scheduling" in out
    assert "❌ Unschedulable" in out
    assert "Insufficient cpu" in out
    assert "cpu=[2]" in out


def test_pending_flags_unbound_pvc():
    pod = _pod(
        phase="Pending",
        node=None,
        volumes=[_pvc_volume("data-p")],
        spec_containers=[_spec_container()],
    )
    core = _FakeCore(pod, pvc_phase="Pending")
    out, _ = _run(core=core)
    assert "PVC data-p → Pending" in out
    assert "not bound" in out


def test_pending_scheduled_but_pending_hint():
    """Scheduled (no unschedulable condition) but still Pending → image/init."""
    pod = _pod(
        phase="Pending",
        node="node-1",
        conditions=[Ns(type="PodScheduled", status="True", reason=None, message=None)],
        spec_containers=[_spec_container()],
    )
    out, _ = _run(pod)
    assert "scheduled but still Pending" in out


def test_requests_none_set_reported():
    pod = _pod(
        phase="Pending",
        node=None,
        spec_containers=[_spec_container()],  # no cpu/memory
    )
    out, _ = _run(pod)
    assert "none set" in out


# ---------- events section --------------------------------------------------


def test_events_section_always_present():
    pod = _pod(containers=[_cs("app")])
    out, _ = _run(pod)
    assert "## Recent events" in out
