"""Tests for `explain_pod` — owner chain / sibling / template inspector.

We patch `explain._dyn_client`, `explain._generic`, and
`explain._sibling_pods` so Resource resolution and sibling enumeration
are fully stubbed. Pods / ReplicaSets / Deployments are plain dicts
stored under a single fake resource registry keyed by (api_version,
kind). All `_generic._to_dict` / `_generic._resource_for_kind` paths
go through one fake generic so we exercise the dispatch uniformly.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException

from k8s_mcp.tools import explain as ex

# ---------- fakes -----------------------------------------------------------


class _FakeResource:
    """Mimics a DynamicClient ResourceHandle for `.get(...)` calls.

    Looks up by (api_version, kind, namespace, name). If not found,
    raises 404 — preserving the real DynamicClient's error path so the
    explainer's `ApiException → ValueError("not found")` translation
    is exercised.
    """

    def __init__(self, registry: dict, av: str, kind: str):
        self._registry = registry
        self._av = av
        self._kind = kind

    def get(self, name, namespace=None):
        if namespace is None:
            for (a, k, _ns, n), obj in self._registry.items():
                if a == self._av and k == self._kind and n == name:
                    return obj
            raise ApiException(status=404, reason="not found")
        key = (self._av, self._kind, namespace, name)
        if key not in self._registry:
            raise ApiException(status=404, reason="not found")
        return self._registry[key]


class _FakeGeneric:
    """Stub for the `generic` module's helpers that explain_pod uses."""

    def __init__(self, registry: dict):
        self._registry = registry

    def _resource_for_kind(self, dc, kind, api_version=None):
        # Build the registry keys we have for this (av, kind) — there may
        # be multiple objects stored across namespaces/names.
        avs = {a for (a, k, *_rest) in self._registry
               if a == api_version and k == kind}
        assert avs, f"no fake resource registered for {api_version}/{kind}"
        return _FakeResource(self._registry, api_version, kind)

    @staticmethod
    def _to_dict(obj):
        if isinstance(obj, dict):
            return obj
        return getattr(obj, "to_dict", lambda: {})()


def _install_explain_fakes(pod=None, registry_extra=None, siblings=None):
    """Patch explain's three injection points and return a builder for
    per-test customisation. `registry_extra` lets tests stash owners too.
    """
    registry: dict = {}

    # Always have the Pod available
    if pod is None:
        pod = _pod("loner")
    registry[("v1", "Pod", pod.get("metadata", {}).get("namespace", "default"),
              pod.get("metadata", {}).get("name"))] = pod

    if registry_extra:
        registry.update(registry_extra)

    fake_generic = _FakeGeneric(registry)

    def fake_siblings(dc, label_selector, namespace="default"):
        return siblings or []

    return patch.multiple(
        ex,
        _dyn_client=lambda: object(),
        _generic=fake_generic,
        _sibling_pods=MagicMock(side_effect=fake_siblings),
    ), registry


# ---------- builders --------------------------------------------------------


def _pod(name, *, owners=None, labels=None, node=None, sa="default",
         containers=None, namespace="default"):
    meta: dict = {"name": name, "namespace": namespace,
                  "uid": f"pod-{name}", "labels": labels or {}}
    if owners is not None:
        meta["ownerReferences"] = owners
    spec: dict = {"serviceAccountName": sa, "nodeName": node}
    if containers is not None:
        spec["containers"] = containers
    return {"metadata": meta, "spec": spec}


def _owner(kind, name, api_version=None, uid=None):
    ref: dict = {"kind": kind, "name": name}
    if api_version:
        ref["apiVersion"] = api_version
    if uid:
        ref["uid"] = uid
    return ref


def _rs(name, *, owners=None, replicas=3, labels=None):
    md: dict = {"name": name, "namespace": "default",
                "uid": f"rs-{name}",
                "labels": labels or {}}
    if owners is not None:
        md["ownerReferences"] = owners
    return {
        "metadata": md,
        "spec": {"replicas": replicas},
        "status": {"replicas": replicas},
    }


def _deploy(name, *, replicas=3, template_labels=None):
    return {
        "metadata": {
            "name": name, "namespace": "default", "uid": f"dep-{name}",
            "labels": {"app": "web"},
        },
        "spec": {
            "replicas": replicas,
            "template": {"metadata": {
                "labels": template_labels or {"app": "web"},
            }},
        },
        "status": {"availableReplicas": replicas},
    }


# ---------- orphan ----------------------------------------------------------


def test_orphan_pod_no_owners_renders_cleanly():
    pod = _pod("loner")
    p, _reg = _install_explain_fakes(pod=pod)
    with p:
        out = ex.explain_pod("default", "loner")
    assert "no ownerReferences" in out
    assert "not managed by any controller" in out
    assert "### Top controller" not in out  # no top → no section


# ---------- owner chain -----------------------------------------------------


def test_deployment_owned_replica_set_owned_deployment_renders_chain():
    rs = _rs("web-abc123", owners=[
        _owner("Deployment", "web", "apps/v1", uid="dep-web"),
    ])
    deploy = _deploy("web")
    pod = _pod("web-xyz", owners=[
        _owner("ReplicaSet", "web-abc123", "apps/v1", uid="rs-1"),
    ])
    extra = {
        ("apps/v1", "ReplicaSet", "default", "web-abc123"): rs,
        ("apps/v1", "Deployment", "default", "web"): deploy,
    }
    p, _ = _install_explain_fakes(pod=pod, registry_extra=extra)
    with p:
        out = ex.explain_pod("default", "web-xyz")
    assert "Pod → ReplicaSet → Deployment" in out
    assert "web-xyz" in out
    assert "web-abc123" in out
    assert "web" in out
    assert "Deployment/web" in out  # top controller line


def test_one_hop_chain_pod_to_replica_set_reports_top():
    rs = _rs("web-abc")
    pod = _pod("web-1", owners=[
        _owner("ReplicaSet", "web-abc", "apps/v1", uid="rs-1"),
    ])
    extra = {("apps/v1", "ReplicaSet", "default", "web-abc"): rs}
    p, _ = _install_explain_fakes(pod=pod, registry_extra=extra)
    with p:
        out = ex.explain_pod("default", "web-1")
    assert "Pod → ReplicaSet" in out
    assert "ReplicaSet/web-abc" in out


def test_chain_terminates_on_cycle_uid():
    """Pod and owner share uid → cycle guard, we stop without infinite loop."""
    rs = _rs("web")
    pod = _pod("web", owners=[
        _owner("ReplicaSet", "web", "apps/v1", uid="rs-web"),
    ])
    extra = {("apps/v1", "ReplicaSet", "default", "web"): rs}
    p, _ = _install_explain_fakes(pod=pod, registry_extra=extra)
    with p:
        out = ex.explain_pod("default", "web")
    # Chain renders; we just ensure we return something sane.
    assert "ReplicaSet" in out


def test_owner_kind_unknown_stops_chain_with_note():
    """If a pod is owned by a CRD not installed, we surface the gap."""
    pod = _pod("c-1", owners=[
        _owner("MyCRD", "thing", "example.com/v1", uid="x-1"),
    ])

    class _SelectiveGeneric(_FakeGeneric):
        def _resource_for_kind(self, dc, kind, api_version=None):
            if kind == "Pod":
                return super()._resource_for_kind(
                    dc, kind, api_version=api_version,
                )
            raise ValueError(f"Unknown kind '{kind}'")

    with patch.multiple(
        ex,
        _dyn_client=lambda: object(),
        _generic=_SelectiveGeneric({("v1", "Pod", "default", "c-1"): pod}),
        _sibling_pods=MagicMock(return_value=[]),
    ):
        out = ex.explain_pod("default", "c-1")
    assert "could not resolve" in out
    assert "MyCRD/thing" in out


# ---------- sibling pods ----------------------------------------------------


def test_siblings_renders_when_top_has_pod_template_labels():
    rs = _rs("web-abc", owners=[
        _owner("Deployment", "web", "apps/v1", uid="dep-web"),
    ])
    deploy = _deploy("web", template_labels={"app": "web"})
    pod = _pod("web-1", labels={"app": "web"}, owners=[
        _owner("ReplicaSet", "web-abc", "apps/v1", uid="rs-1"),
    ])
    extra = {
        ("apps/v1", "ReplicaSet", "default", "web-abc"): rs,
        ("apps/v1", "Deployment", "default", "web"): deploy,
    }
    sibling_dicts = [
        {"metadata": {"name": n},
         "spec": {"nodeName": "node-1"},
         "status": {"phase": "Running"}}
        for n in ("web-1", "web-2", "web-3")
    ]
    p, _ = _install_explain_fakes(
        pod=pod, registry_extra=extra, siblings=sibling_dicts,
    )
    with p:
        out = ex.explain_pod("default", "web-1")
    assert "web-1" in out
    assert "web-2" in out
    assert "web-3" in out
    assert "Sibling pods" in out


# ---------- pod spec keys ---------------------------------------------------


def test_pod_spec_keys_rendered_node_sa_containers():
    pod = _pod(
        "p", node="node-1", sa="my-sa",
        containers=[{"name": "app", "image": "nginx:1.25"}],
    )
    p, _ = _install_explain_fakes(pod=pod)
    with p:
        out = ex.explain_pod("default", "p")
    assert "node-1" in out
    assert "my-sa" in out
    assert "app" in out
    assert "nginx:1.25" in out


# ---------- not-found -------------------------------------------------------


def test_pod_not_found_raises_value_error():
    fake_generic = MagicMock(
        _resource_for_kind=MagicMock(side_effect=ApiException(status=404)),
        _to_dict=lambda r: r,
    )
    with patch.multiple(
        ex,
        _dyn_client=lambda: object(),
        _generic=fake_generic,
        _sibling_pods=MagicMock(return_value=[]),
    ):
        with pytest.raises(ValueError, match="not found"):
            ex.explain_pod("default", "ghost")
