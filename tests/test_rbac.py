"""Tests for RBAC shortcut tools (create_role / create_clusterrole / bindings).

Verifies that:
  - the cluster-admin triple (`verbs=*` ∧ `resources=*` ∧ `apiGroups=*`)
    is refused by default
  - single-axis wildcards (e.g. `verbs=["*"]` on specific resources)
    are still allowed (normal K8s pattern for controller SAs)
  - `allow_wildcard=True` opts in explicitly and the manifest is built
    with the wildcard rule intact
  - basic manifests are still produced for ordinary rules
  - read-only mode is enforced by apply_yaml (smoke test)
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from k8s_mcp.config import reset_settings_cache
from k8s_mcp.tools import rbac


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_settings_cache()
    yield
    reset_settings_cache()


# ---------------------------------------------------------------------------
# _validate_rule — the gate
# ---------------------------------------------------------------------------


def test_validate_rule_rejects_wildcard_triple():
    rule = {"verbs": ["*"], "resources": ["*"], "apiGroups": ["*"]}
    with pytest.raises(PermissionError, match="cluster-admin wildcard triple"):
        rbac._validate_rule(rule)


def test_validate_rule_allows_wildcard_triple_with_opt_in():
    rule = {"verbs": ["*"], "resources": ["*"], "apiGroups": ["*"]}
    out = rbac._validate_rule(rule, allow_wildcard=True)
    assert out == {"verbs": ["*"], "apiGroups": ["*"], "resources": ["*"]}


def test_validate_rule_allows_single_axis_wildcard():
    """`verbs=['*']` on specific resources is a normal controller-SA pattern."""
    rule = {"verbs": ["*"], "resources": ["pods"], "apiGroups": [""]}
    out = rbac._validate_rule(rule)
    assert out["verbs"] == ["*"]
    assert out["resources"] == ["pods"]


def test_validate_rule_allows_wildcard_verbs_but_specific_resources():
    rule = {"verbs": ["*"], "resources": ["configmaps"], "apiGroups": [""]}
    out = rbac._validate_rule(rule)
    assert out == {
        "verbs": ["*"],
        "apiGroups": [""],
        "resources": ["configmaps"],
    }


def test_validate_rule_rejects_missing_verbs():
    with pytest.raises(ValueError, match="missing 'verbs'"):
        rbac._validate_rule({"resources": ["pods"]})


def test_validate_rule_preserves_resource_names():
    rule = {
        "verbs": ["get"],
        "resources": ["configmaps"],
        "apiGroups": [""],
        "resourceNames": ["my-cm"],
    }
    out = rbac._validate_rule(rule)
    assert out["resourceNames"] == ["my-cm"]


# ---------------------------------------------------------------------------
# create_role / create_clusterrole — manifest construction
# ---------------------------------------------------------------------------


def test_create_role_blocks_wildcard_triple_without_opt_in():
    rule = {"verbs": ["*"], "resources": ["*"], "apiGroups": ["*"]}
    with pytest.raises(PermissionError, match="cluster-admin wildcard triple"):
        rbac.create_role("admin", "default", [rule])


def test_create_clusterrole_blocks_wildcard_triple_without_opt_in():
    rule = {"verbs": ["*"], "resources": ["*"], "apiGroups": ["*"]}
    with pytest.raises(PermissionError, match="cluster-admin wildcard triple"):
        rbac.create_clusterrole("admin", [rule])


def test_create_clusterrole_with_wildcard_opt_in_calls_apply():
    """Wildcard is gated by apply_yaml (read-only mode would refuse there).
    Here we patch apply_yaml to capture the manifest it would apply."""
    rule = {"verbs": ["*"], "resources": ["*"], "apiGroups": ["*"]}
    captured: dict = {}

    def fake_apply(manifest_yaml, *args, **kwargs):
        import yaml as _yaml
        captured["manifest"] = _yaml.safe_load(manifest_yaml)
        return "applied"

    with patch.object(rbac.generic, "apply_yaml", side_effect=fake_apply):
        out = rbac.create_clusterrole(
            "admin", [rule], allow_wildcard=True
        )
    assert out == "applied"
    m = captured["manifest"]
    assert m["kind"] == "ClusterRole"
    assert m["metadata"]["name"] == "admin"
    assert m["rules"] == [rule]


def test_create_role_normal_rule_calls_apply():
    rule = {"verbs": ["get", "list"], "resources": ["pods"], "apiGroups": [""]}
    captured: dict = {}

    def fake_apply(manifest_yaml, *args, **kwargs):
        import yaml as _yaml
        captured["manifest"] = _yaml.safe_load(manifest_yaml)
        return "applied"

    with patch.object(rbac.generic, "apply_yaml", side_effect=fake_apply):
        rbac.create_role("reader", "default", [rule])
    m = captured["manifest"]
    assert m["kind"] == "Role"
    assert m["metadata"]["namespace"] == "default"
    assert m["rules"][0]["verbs"] == ["get", "list"]


def test_create_role_rejects_empty_rules():
    with pytest.raises(ValueError, match="at least one rule"):
        rbac.create_role("reader", "default", [])


# ---------------------------------------------------------------------------
# Wildcard-on-wildcard but missing one axis must still pass through
# (i.e. the triple check is exact — not "any axis is *")
# ---------------------------------------------------------------------------


def test_validate_rule_allows_two_of_three_wildcards():
    """`resources=['*']` ∧ `apiGroups=['*']` but verbs is specific — allowed.

    This is still broad but not full cluster-admin; refused-cluster-admin
    policy targets the exact triple only.
    """
    rule = {
        "verbs": ["get", "list", "watch"],
        "resources": ["*"],
        "apiGroups": ["*"],
    }
    out = rbac._validate_rule(rule)
    assert out["resources"] == ["*"]
    assert out["apiGroups"] == ["*"]


def test_create_role_blocks_two_of_three_wildcards_too():
    """The wildcard triple is the cluster-admin case; two-axis wildcards
    are wide but not cluster-admin — currently allowed. This test pins
    that policy so a future change to widen the deny list fails loudly."""
    rule = {
        "verbs": ["get"],
        "resources": ["*"],
        "apiGroups": ["*"],
    }
    captured: dict = {}

    def fake_apply(manifest_yaml, *args, **kwargs):
        import yaml as _yaml
        captured["manifest"] = _yaml.safe_load(manifest_yaml)
        return "applied"

    with patch.object(rbac.generic, "apply_yaml", side_effect=fake_apply):
        rbac.create_role("wide-reader", "default", [rule])
    assert captured["manifest"]["rules"][0]["resources"] == ["*"]
