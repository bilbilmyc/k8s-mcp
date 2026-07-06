"""Tests for `diagnose_deployment` — one-shot Deployment triage.

We stub `diagnostics._apps_v1` + `diagnostics._core_v1` with fakes that
mirror the apps/v1 + core/v1 surface area the function actually touches,
and patch `events_mod.get_events_for_object`. Deployment / ReplicaSet /
Pod / Event objects are built from `SimpleNamespace` to mirror the
attribute shape of the kubernetes client models (same trick as
test_diagnose_pod.py).
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace as Ns
from unittest.mock import patch

import pytest
from kubernetes.client.rest import ApiException

from k8s_mcp.tools import diagnostics

# ---------- builders --------------------------------------------------------


def _container(name="app", image="nginx:1.25"):
    return Ns(name=name, image=image)


def _pod_template(labels: dict, image: str = "nginx:1.25"):
    return Ns(
        metadata=Ns(labels=labels),
        spec=Ns(containers=[_container(image=image)]),
    )


def _deployment(
    *,
    name="web",
    namespace="default",
    replicas=3,
    strategy="RollingUpdate",
    match_labels=None,
    status=None,
    age=datetime(2026, 7, 6, tzinfo=UTC),
):
    # NOTE: callers can pass match_labels={} to exercise the empty-selector
    # path; don't coalesce empty dict into the default.
    if match_labels is None:
        match_labels = {"app": "web"}
    spec = Ns(
        replicas=replicas,
        selector=Ns(match_labels=match_labels),
        strategy=Ns(type=strategy),
        template=_pod_template(match_labels),
    )
    return Ns(
        metadata=Ns(name=name, namespace=namespace, creation_timestamp=age),
        spec=spec,
        status=status or Ns(
            replicas=replicas,
            ready_replicas=replicas,
            updated_replicas=replicas,
            available_replicas=replicas,
            conditions=[],
        ),
    )


def _rs(
    *,
    name,
    hash_label,
    desired=3,
    current=3,
    ready=3,
    image="nginx:1.25",
    match_labels=None,
):
    match_labels = match_labels or {"app": "web"}
    return Ns(
        metadata=Ns(name=name, namespace="default"),
        spec=Ns(
            replicas=desired,
            selector=Ns(match_labels=match_labels),
            template=_pod_template(
                {**match_labels, "pod-template-hash": hash_label},
                image=image,
            ),
        ),
        status=Ns(replicas=current, ready_replicas=ready),
    )


def _pod(name, phase="Running", restarts=0, node="node-1", hash_label="aaaa"):
    return Ns(
        metadata=Ns(
            name=name,
            namespace="default",
            creation_timestamp=datetime(2026, 7, 6, tzinfo=UTC),
            labels={"pod-template-hash": hash_label},
        ),
        status=Ns(phase=phase, container_statuses=[
            Ns(restart_count=restarts, state=Ns(waiting=None, running=Ns(), terminated=None))
        ] if phase == "Running" else [
            Ns(restart_count=restarts, state=Ns(
                waiting=Ns(reason="CrashLoopBackOff") if phase == "CrashLoop" else None,
                running=None,
                terminated=None,
            ))
        ]),
        spec=Ns(node_name=node),
    )


# ---------- fakes ----------------------------------------------------------


class _FakeApps:
    def __init__(
        self, deployment=None, replicasets=None, *, read_raises=None, list_raises=None,
    ):
        self._deployment = deployment
        self._replicasets = replicasets or []
        self._read_raises = read_raises
        self._list_raises = list_raises

    def read_namespaced_deployment(self, name, namespace):
        if self._read_raises is not None:
            raise self._read_raises
        return self._deployment

    def list_namespaced_replica_set(self, namespace, label_selector=None):
        if self._list_raises is not None:
            raise self._list_raises
        return Ns(items=self._replicasets)


class _FakeCore:
    def __init__(self, pods=None, *, list_raises=None):
        self._pods = pods or []
        self._list_raises = list_raises

    def list_namespaced_pod(self, namespace, label_selector=None):
        if self._list_raises is not None:
            raise self._list_raises
        return Ns(items=self._pods)


def _run(deployment=None, *, apps=None, core=None):
    """Invoke diagnose_deployment with all external deps stubbed."""
    apps = apps or _FakeApps(deployment=deployment)
    core = core or _FakeCore()
    with patch.object(diagnostics, "_apps_v1", return_value=apps), \
            patch.object(diagnostics, "_core_v1", return_value=core), \
            patch.object(
                diagnostics.events_mod, "get_events_for_object",
                return_value="(no events for Deployment/web in namespace default)",
            ):
        return diagnostics.diagnose_deployment("web", "default")


# ---------- lookup errors --------------------------------------------------


def test_deployment_not_found_raises_value_error():
    apps = _FakeApps(read_raises=ApiException(status=404, reason="not found"))
    with pytest.raises(ValueError, match="not found"):
        _run(apps=apps)


def test_deployment_read_other_error_raises_runtime():
    apps = _FakeApps(read_raises=ApiException(status=403, reason="forbidden"))
    with pytest.raises(RuntimeError, match="403"):
        _run(apps=apps)


# ---------- healthy rollout ------------------------------------------------


def test_healthy_rollout_reports_complete_progressing():
    dep = _deployment(status=Ns(
        replicas=3, ready_replicas=3, updated_replicas=3, available_replicas=3,
        conditions=[
            Ns(
                type="Progressing", status="True",
                reason="NewReplicaSetAvailable",
                message="Rollout complete",
            )
        ],
    ))
    rs_old = _rs(name="web-oldaaa", hash_label="aaa", desired=0, current=0, ready=0)
    rs_new = _rs(name="web-newbbb", hash_label="bbb", desired=3, current=3, ready=3)
    apps = _FakeApps(deployment=dep, replicasets=[rs_old, rs_new])
    pods = [_pod(f"web-newbbb-{i}", hash_label="bbb") for i in range(3)]
    core = _FakeCore(pods=pods)
    out = _run(apps=apps, core=core)

    assert "Strategy: RollingUpdate" in out
    assert "desired=3" in out
    assert "ready=3" in out
    assert "✅ NewReplicaSetAvailable" in out
    assert "## ReplicaSets" in out
    assert "web-newbbb" in out
    assert "web-oldaaa" in out
    # Newest first
    new_idx = out.index("web-newbbb")
    old_idx = out.index("web-oldaaa")
    assert new_idx < old_idx
    assert "## New ReplicaSet" in out
    assert "ready 3/3" in out
    assert "## Recent events" in out
    # No problem pods → no "next step" hint
    assert "Next step" not in out


def test_rollout_in_progress_observed_gen_mismatch():
    dep = _deployment(status=Ns(
        replicas=3, ready_replicas=2, updated_replicas=1, available_replicas=2,
        conditions=[
            Ns(
                type="Progressing", status="True",
                reason="ReplicaSetUpdated",
                message="replicas updated",
            )
        ],
    ))
    rs_new = _rs(name="web-newccc", hash_label="ccc", desired=3, current=2, ready=2)
    apps = _FakeApps(deployment=dep, replicasets=[rs_new])
    pods = [_pod(f"web-newccc-{i}", hash_label="ccc") for i in range(2)]
    out = _run(apps=apps, core=_FakeCore(pods=pods))
    assert "ready=2" in out
    assert "ReplicaSetUpdated" in out


# ---------- stale / failed rollout ----------------------------------------


def test_progress_deadline_exceeded_is_surfaced():
    dep = _deployment(status=Ns(
        replicas=3, ready_replicas=1, updated_replicas=2, available_replicas=1,
        conditions=[
            Ns(
                type="Progressing", status="False",
                reason="ProgressDeadlineExceeded",
                message="progress deadline exceeded",
            )
        ],
    ))
    rs_new = _rs(name="web-newddd", hash_label="ddd", desired=3, current=2, ready=1)
    apps = _FakeApps(deployment=dep, replicasets=[rs_new])
    pods = [_pod("web-newddd-0", hash_label="ddd")] + [
        _pod(f"web-newddd-{i}", phase="Pending", hash_label="ddd") for i in range(1, 3)
    ]
    out = _run(apps=apps, core=_FakeCore(pods=pods))
    assert "❌" in out
    assert "ProgressDeadlineExceeded" in out
    # Problem pods → "next step" hint points at diagnose_pod
    assert "Next step" in out
    assert "diagnose_pod" in out
    assert "web-newddd-1" in out  # first Pending pod


def test_old_replica_set_scaled_to_zero_listed():
    """A healthy rollout shows the OLD RS with desired=0 — the user
    should still see both to confirm the controller is doing its job."""
    dep = _deployment()
    rs_old = _rs(name="web-oldxxx", hash_label="xxx", desired=0, current=0, ready=0)
    rs_new = _rs(name="web-newyyy", hash_label="yyy", desired=3, current=3, ready=3)
    apps = _FakeApps(deployment=dep, replicasets=[rs_old, rs_new])
    pods = [_pod(f"web-newyyy-{i}", hash_label="yyy") for i in range(3)]
    out = _run(apps=apps, core=_FakeCore(pods=pods))
    assert "web-oldxxx" in out
    # desired=0 visible for the old RS
    old_line = [line for line in out.splitlines() if "web-oldxxx" in line][0]
    assert "0" in old_line.split("web-oldxxx")[1][:20]


def test_image_diff_visible_in_rs_table():
    """If the new RS rolled out a different image, the table row should
    reflect that without any extra call from the agent."""
    dep = _deployment()
    rs_old = _rs(name="web-oldimg", hash_label="aaa", image="nginx:1.24", desired=0, current=0, ready=0)
    rs_new = _rs(name="web-newimg", hash_label="bbb", image="nginx:1.25", desired=3, current=3, ready=3)
    apps = _FakeApps(deployment=dep, replicasets=[rs_old, rs_new])
    pods = [_pod(f"web-newimg-{i}", hash_label="bbb") for i in range(3)]
    out = _run(apps=apps, core=_FakeCore(pods=pods))
    assert "nginx:1.24" in out
    assert "nginx:1.25" in out


# ---------- empty / unusual states ----------------------------------------


def test_no_replica_sets_reports_soft_message():
    dep = _deployment(match_labels={"app": "web"})
    apps = _FakeApps(deployment=dep, replicasets=[])
    out = _run(apps=apps)
    assert "## ReplicaSets" in out
    assert "no ReplicaSets found" in out
    # No "New ReplicaSet" section when there's no RS
    assert "## New ReplicaSet" not in out


def test_deployment_with_empty_selector_no_rs_crash():
    """A pathological Deployment with no matchLabels shouldn't crash —
    the function should fall through to the empty-RS path."""
    dep = _deployment(match_labels={})
    apps = _FakeApps(deployment=dep, replicasets=[])
    out = _run(apps=apps)
    assert "Selector: (empty)" in out
    assert "no ReplicaSets found" in out


def test_pod_list_failure_is_soft():
    """If the pod list API errors, we should not crash the report —
    surface a one-liner and keep the rollout / RS sections intact."""
    dep = _deployment()
    rs_new = _rs(name="web-new", hash_label="nnn", desired=3, current=3, ready=3)
    apps = _FakeApps(deployment=dep, replicasets=[rs_new])
    core = _FakeCore(list_raises=ApiException(status=500, reason="server error"))
    out = _run(apps=apps, core=core)
    assert "failed to list pods" in out
    # Rollout section still present
    assert "## Rollout" in out


# ---------- events section -------------------------------------------------


def test_events_section_always_present():
    dep = _deployment()
    rs_new = _rs(name="web-new", hash_label="hhh", desired=3, current=3, ready=3)
    apps = _FakeApps(deployment=dep, replicasets=[rs_new])
    out = _run(apps=apps, core=_FakeCore(pods=[]))
    assert "## Recent events" in out
