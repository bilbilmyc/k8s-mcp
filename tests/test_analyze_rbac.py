"""Tests for `analyze_rbac` — the multi-mode read-only RBAC inspector.

We patch `rbac._dyn_client` + `rbac._rbac_resource` so the four RBAC kinds
(Role / RoleBinding / ClusterRole / ClusterRoleBinding) resolve to fake
resource handles whose `.get(...)` returns plain dicts. The render layer
only ever touches items via `.get(...)`, so dicts stand in cleanly.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from kubernetes.dynamic.exceptions import ResourceNotFoundError

from k8s_mcp.tools import rbac

# ---------- fakes -----------------------------------------------------------


class _FakeReturn:
    def __init__(self, items: list[dict]):
        self.items = items


class _FakeResource:
    def __init__(self, items: list[dict]):
        self._items = items

    def get(self, namespace=None):
        return _FakeReturn(self._items)


def _patch_rbac(roles=None, bindings=None, cluster_roles=None, cluster_bindings=None):
    mapping = {
        "Role": _FakeResource(roles or []),
        "RoleBinding": _FakeResource(bindings or []),
        "ClusterRole": _FakeResource(cluster_roles or []),
        "ClusterRoleBinding": _FakeResource(cluster_bindings or []),
    }

    def fake_rbac_resource(dc, kind):
        return mapping[kind]

    return patch.multiple(
        rbac,
        _dyn_client=lambda: object(),
        _rbac_resource=fake_rbac_resource,
    )


# ---------- builders --------------------------------------------------------


def _rule(verbs, resources, api_groups=None):
    return {"verbs": verbs, "resources": resources, "apiGroups": api_groups or [""]}


def _role(name, rules, namespace=None, kind="Role"):
    meta: dict = {"name": name}
    if namespace:
        meta["namespace"] = namespace
    return {"kind": kind, "metadata": meta, "rules": rules}


def _subject(kind, name, namespace=None):
    s: dict = {"kind": kind, "name": name}
    if namespace:
        s["namespace"] = namespace
    return s


def _binding(name, role_kind, role_name, subjects, namespace=None, kind="RoleBinding"):
    meta: dict = {"name": name}
    if namespace:
        meta["namespace"] = namespace
    return {
        "kind": kind,
        "metadata": meta,
        "roleRef": {"kind": role_kind, "name": role_name},
        "subjects": subjects,
    }


# ---------- _subject_matches ------------------------------------------------


def test_subject_matches_exact_name():
    assert rbac._subject_matches({"kind": "User", "name": "alice"}, "alice")


def test_subject_matches_fully_qualified_form():
    s = {"kind": "Group", "name": "system:masters"}
    assert rbac._subject_matches(s, "Group:system:masters")


def test_subject_matches_rejects_other_name():
    assert not rbac._subject_matches({"kind": "User", "name": "bob"}, "alice")


def test_subject_matches_empty_needle_is_false():
    assert not rbac._subject_matches({"kind": "User", "name": "alice"}, "")


# ---------- _rule_matches ---------------------------------------------------


def test_rule_matches_verb_resource_group():
    r = _rule(["get", "list"], ["pods"], [""])
    assert rbac._rule_matches(r, "get", "pods", "")


def test_rule_matches_verb_only():
    r = _rule(["get", "list"], ["pods"], [""])
    assert rbac._rule_matches(r, "get", None, None)


def test_rule_matches_rejects_missing_verb():
    r = _rule(["get"], ["pods"], [""])
    assert not rbac._rule_matches(r, "delete", "pods", "")


def test_rule_matches_rejects_wrong_resource():
    r = _rule(["get"], ["pods"], [""])
    assert not rbac._rule_matches(r, "get", "secrets", "")


def test_rule_matches_wildcard_rule_matches_anything():
    w = _rule(["*"], ["*"], ["*"])
    assert rbac._rule_matches(w, "delete", "anything", "apps")


def test_rule_matches_none_api_group_skips_group_filter():
    r = _rule(["get"], ["pods"], [""])
    assert rbac._rule_matches(r, "get", "pods", None)


def test_rule_matches_rejects_wrong_api_group():
    r = _rule(["get"], ["pods"], [""])
    assert not rbac._rule_matches(r, "get", "pods", "apps")


# ---------- Mode 1: subject → forward lookup --------------------------------


def test_subject_mode_lists_granted_rules():
    role = _role("reader", [_rule(["get", "list"], ["pods"], [""])], kind="ClusterRole")
    binding = _binding(
        "bind-alice", "ClusterRole", "reader",
        [_subject("User", "alice")], kind="ClusterRoleBinding",
    )
    with _patch_rbac(cluster_roles=[role], cluster_bindings=[binding]):
        out = rbac.analyze_rbac(subject="alice")

    assert "subject = 'alice'" in out
    assert "Matched 1 binding" in out
    assert "get" in out
    assert "pods" in out


def test_subject_mode_no_binding():
    with _patch_rbac():
        out = rbac.analyze_rbac(subject="ghost")
    assert "no RoleBinding" in out


def test_subject_mode_binding_references_missing_role():
    binding = _binding(
        "b", "ClusterRole", "gone",
        [_subject("User", "alice")], kind="ClusterRoleBinding",
    )
    with _patch_rbac(cluster_bindings=[binding]):
        out = rbac.analyze_rbac(subject="alice")
    assert "not found in scope" in out


def test_subject_mode_flags_wildcards():
    role = _role("admin", [_rule(["*"], ["*"], ["*"])], kind="ClusterRole")
    binding = _binding(
        "b", "ClusterRole", "admin",
        [_subject("User", "alice")], kind="ClusterRoleBinding",
    )
    with _patch_rbac(cluster_roles=[role], cluster_bindings=[binding]):
        out = rbac.analyze_rbac(subject="alice")
    assert "cluster-admin risk" in out


# ---------- Mode 2: verb + resource → reverse lookup ------------------------


def test_reverse_mode_finds_subjects_with_access():
    role = _role("deleter", [_rule(["delete"], ["pods"], [""])], kind="ClusterRole")
    binding = _binding(
        "b", "ClusterRole", "deleter",
        [_subject("ServiceAccount", "app", "default")], kind="ClusterRoleBinding",
    )
    with _patch_rbac(cluster_roles=[role], cluster_bindings=[binding]):
        out = rbac.analyze_rbac(verb="delete", resource="pods")

    assert "who can" in out
    assert "ServiceAccount:app" in out
    assert "deleter" in out


def test_reverse_mode_no_matching_role():
    role = _role("reader", [_rule(["get"], ["pods"], [""])], kind="ClusterRole")
    with _patch_rbac(cluster_roles=[role]):
        out = rbac.analyze_rbac(verb="delete", resource="secrets")
    assert "no role grants this action" in out


def test_reverse_mode_role_without_binding_is_unreachable():
    role = _role("deleter", [_rule(["delete"], ["pods"], [""])], kind="ClusterRole")
    with _patch_rbac(cluster_roles=[role]):
        out = rbac.analyze_rbac(verb="delete", resource="pods")
    assert "unreachable" in out or "unused" in out
    assert "deleter" in out


def test_reverse_mode_matches_wildcard_rule_and_flags_it():
    role = _role("admin", [_rule(["*"], ["*"], ["*"])], kind="ClusterRole")
    binding = _binding(
        "b", "ClusterRole", "admin",
        [_subject("User", "root")], kind="ClusterRoleBinding",
    )
    with _patch_rbac(cluster_roles=[role], cluster_bindings=[binding]):
        out = rbac.analyze_rbac(verb="delete", resource="pods")
    assert "User:root" in out
    assert "wildcard" in out.lower()


# ---------- Mode 3: namespace only ------------------------------------------


def test_namespace_mode_lists_roles_and_bindings():
    role = _role("reader", [_rule(["get"], ["pods"], [""])], namespace="team-a")
    binding = _binding(
        "b", "Role", "reader",
        [_subject("User", "alice")], namespace="team-a",
    )
    with _patch_rbac(roles=[role], bindings=[binding]):
        out = rbac.analyze_rbac(namespace="team-a")

    assert "RBAC in namespace 'team-a'" in out
    assert "reader" in out
    assert "User:alice" in out


def test_namespace_mode_empty():
    with _patch_rbac():
        out = rbac.analyze_rbac(namespace="empty-ns")
    assert "no RBAC objects" in out


# ---------- Mode 4: cluster-wide summary ------------------------------------


def test_summary_mode_counts_and_flags_wildcards():
    crole = _role("admin", [_rule(["*"], ["*"], ["*"])], kind="ClusterRole")
    cbind = _binding(
        "b", "ClusterRole", "admin",
        [_subject("User", "root")], kind="ClusterRoleBinding",
    )
    nrole = _role("reader", [_rule(["get"], ["pods"], [""])], namespace="ns1")
    nbind = _binding(
        "nb", "Role", "reader",
        [_subject("User", "alice")], namespace="ns1",
    )
    with _patch_rbac(
        cluster_roles=[crole], cluster_bindings=[cbind],
        roles=[nrole], bindings=[nbind],
    ):
        out = rbac.analyze_rbac()

    assert "RBAC summary" in out
    assert "ClusterRoles: 1" in out
    assert "ClusterRoleBindings: 1" in out
    assert "Roles (across all namespaces): 1" in out
    assert "RoleBindings (across all namespaces): 1" in out
    assert "cluster-admin risk surface" in out


def test_summary_mode_clean_surface():
    crole = _role("reader", [_rule(["get"], ["pods"], [""])], kind="ClusterRole")
    with _patch_rbac(cluster_roles=[crole]):
        out = rbac.analyze_rbac()
    assert "clean RBAC surface" in out


# ---------- API availability ------------------------------------------------


def test_missing_rbac_api_raises_runtime():
    def boom(dc, kind):
        raise ResourceNotFoundError("nope")

    with patch.object(rbac, "_dyn_client", lambda: object()), \
            patch.object(rbac, "_rbac_resource", boom):
        with pytest.raises(RuntimeError, match="RBAC"):
            rbac.analyze_rbac()
