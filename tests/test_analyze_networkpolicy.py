"""Tests for `analyze_networkpolicy` — connectivity / coverage inspector.

We patch `networkpolicy._dyn_client` + `_np_resource` + `_pod_resource`
so NetworkPolicy / Pod objects resolve to fake resource handles returning
plain dicts (the render layer only touches `.get(...)`).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from kubernetes.client.rest import ApiException
from kubernetes.dynamic.exceptions import NotFoundError

from k8s_mcp.tools import networkpolicy as np

# ---------- fakes -----------------------------------------------------------


class _FakeReturn:
    def __init__(self, items):
        self.items = items


class _FakeNP:
    def __init__(self, policies):
        self._policies = policies

    def get(self, namespace=None):
        return _FakeReturn(self._policies)


class _FakePodRes:
    def __init__(self, pods, by_name):
        self._pods = pods
        self._by_name = by_name

    def get(self, namespace=None, name=None):
        if name is not None:
            if name not in self._by_name:
                raise NotFoundError(ApiException(status=404, reason="not found"))
            return self._by_name[name]
        return _FakeReturn(self._pods)


def _patch(policies=None, pods=None, by_name=None):
    np_res = _FakeNP(policies or [])
    pod_res = _FakePodRes(pods or [], by_name or {})
    return patch.multiple(
        np,
        _dyn_client=lambda: object(),
        _np_resource=lambda dc: np_res,
        _pod_resource=lambda dc: pod_res,
    )


# ---------- builders --------------------------------------------------------


def _policy(name, *, pod_selector=None, policy_types=None, ingress=None, egress=None):
    spec: dict = {"podSelector": {} if pod_selector is None else pod_selector}
    if policy_types is not None:
        spec["policyTypes"] = policy_types
    if ingress is not None:
        spec["ingress"] = ingress
    if egress is not None:
        spec["egress"] = egress
    return {"metadata": {"name": name, "namespace": "default"}, "spec": spec}


def _pod(name, labels=None):
    return {"metadata": {"name": name, "namespace": "default", "labels": labels or {}}}


# ---------- _selector_matches / _expr_matches / _policy_types ----------------


def test_empty_selector_matches_all():
    assert np._selector_matches({}, {"app": "x"})
    assert np._selector_matches(None, {})


def test_match_labels_subset():
    assert np._selector_matches({"matchLabels": {"app": "web"}}, {"app": "web", "t": "1"})
    assert not np._selector_matches({"matchLabels": {"app": "web"}}, {"app": "db"})


def test_expr_in_and_notin():
    labels = {"tier": "frontend"}
    assert np._expr_matches({"key": "tier", "operator": "In", "values": ["frontend"]}, labels)
    assert not np._expr_matches({"key": "tier", "operator": "In", "values": ["backend"]}, labels)
    assert np._expr_matches({"key": "tier", "operator": "NotIn", "values": ["backend"]}, labels)


def test_expr_exists_and_doesnotexist():
    labels = {"tier": "frontend"}
    assert np._expr_matches({"key": "tier", "operator": "Exists"}, labels)
    assert not np._expr_matches({"key": "role", "operator": "Exists"}, labels)
    assert np._expr_matches({"key": "role", "operator": "DoesNotExist"}, labels)


def test_policy_types_inference():
    assert np._policy_types(_policy("p")) == ["Ingress"]
    assert np._policy_types(_policy("p", egress=[{"to": []}])) == ["Ingress", "Egress"]
    assert np._policy_types(
        _policy("p", policy_types=["Egress"])
    ) == ["Egress"]


# ---------- pod view --------------------------------------------------------


def test_pod_not_found_raises_value_error():
    with _patch(policies=[], pods=[], by_name={}):
        with pytest.raises(ValueError, match="not found"):
            np.analyze_networkpolicy("default", pod="ghost")


def test_pod_selected_by_zero_policies_is_default_allow():
    pod = _pod("web-1", {"app": "web"})
    other = _policy("db-policy", pod_selector={"matchLabels": {"app": "db"}},
                    policy_types=["Ingress"])
    with _patch(policies=[other], by_name={"web-1": pod}):
        out = np.analyze_networkpolicy("default", pod="web-1")
    assert "Selected by 0 policies" in out
    assert "Ingress: 🔓 default-allow" in out
    assert "Egress:  🔓 default-allow" in out


def test_pod_isolated_ingress_when_selecting_policy_has_ingress_type():
    pod = _pod("web-1", {"app": "web"})
    pol = _policy("deny-ingress", pod_selector={}, policy_types=["Ingress"])
    with _patch(policies=[pol], by_name={"web-1": pod}):
        out = np.analyze_networkpolicy("default", pod="web-1")
    assert "Selected by 1 policy" in out
    assert "(none — deny all ingress)" in out
    assert "Ingress: 🔒 default-deny" in out
    # egress not controlled by this policy → still open
    assert "Egress:  🔓 default-allow" in out


def test_pod_ingress_rule_peers_and_ports_rendered():
    pod = _pod("web-1", {"app": "web"})
    pol = _policy(
        "allow-gw",
        pod_selector={"matchLabels": {"app": "web"}},
        policy_types=["Ingress"],
        ingress=[{
            "from": [{"podSelector": {"matchLabels": {"app": "gw"}}}],
            "ports": [{"protocol": "TCP", "port": 8080}],
        }],
    )
    with _patch(policies=[pol], by_name={"web-1": pod}):
        out = np.analyze_networkpolicy("default", pod="web-1")
    assert "podSelector app=gw" in out
    assert "TCP/8080" in out


def test_pod_matchexpressions_selection():
    pod = _pod("web-1", {"tier": "frontend"})
    pol = _policy(
        "fe-policy",
        pod_selector={"matchExpressions": [
            {"key": "tier", "operator": "In", "values": ["frontend"]},
        ]},
        policy_types=["Egress"],
    )
    with _patch(policies=[pol], by_name={"web-1": pod}):
        out = np.analyze_networkpolicy("default", pod="web-1")
    assert "Selected by 1 policy" in out
    assert "Egress:  🔒 default-deny" in out


# ---------- coverage view ---------------------------------------------------


def test_coverage_no_policies_warns_fully_open():
    with _patch(policies=[], pods=[_pod("a"), _pod("b")]):
        out = np.analyze_networkpolicy("default")
    assert "No NetworkPolicy in this namespace" in out
    assert "default-allow" in out


def test_coverage_marks_open_vs_covered_pods():
    covered = _pod("web-1", {"app": "web"})
    exposed = _pod("db-1", {"app": "db"})
    pol = _policy("web-ingress", pod_selector={"matchLabels": {"app": "web"}},
                  policy_types=["Ingress"])
    with _patch(policies=[pol], pods=[covered, exposed]):
        out = np.analyze_networkpolicy("default")
    assert "1 pod(s) with open ingress" in out
    assert "web-1" in out
    assert "db-1" in out
    assert "🔒 covered" in out
    assert "🔓 open" in out


def test_coverage_policy_inventory_shows_deny_all():
    pol = _policy("deny-all", pod_selector={}, policy_types=["Ingress"])
    with _patch(policies=[pol], pods=[_pod("a")]):
        out = np.analyze_networkpolicy("default")
    assert "### Policies" in out
    assert "deny-all" in out


# ---------- API availability ------------------------------------------------


def test_missing_networkpolicy_api_raises_runtime():
    from kubernetes.dynamic.exceptions import ResourceNotFoundError

    def boom(dc):
        raise ResourceNotFoundError("nope")

    with patch.object(np, "_dyn_client", lambda: object()), \
            patch.object(np, "_np_resource", boom):
        with pytest.raises(RuntimeError, match="NetworkPolicy"):
            np.analyze_networkpolicy("default")
