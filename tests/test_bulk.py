"""Tests for bulk operations.

Strategy: mock the dynamic client at the `generic._dyn_client` /
`generic._resource_for_kind` boundary and verify:
  - dry_run returns preview with no token
  - confirm=False returns preview + token
  - confirm=True applies only to originally-matched resources
  - read-only and allowlist are enforced
  - token's per-call fields (image, replicas, etc.) are checked
  - container-not-found error is clean
  - bulk_scale rejects DaemonSet / negative replicas
  - bulk_restart stamps the restartedAt annotation
"""
from __future__ import annotations

import pytest

from k8s_mcp.config import reset_settings_cache
from k8s_mcp.safety import TokenError
from k8s_mcp.tools import bulk

# ---------- fake dynamic-client model ------------------------------------


def _deploy(ns, name, image="nginx:1.25"):
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": ns},
        "spec": {
            "replicas": 2,
            "template": {
                "metadata": {"annotations": {}},
                "spec": {"containers": [
                    {"name": "app", "image": image},
                ]},
            },
        },
    }


def _sts(ns, name, image="redis:7"):
    return {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {"name": name, "namespace": ns},
        "spec": {
            "replicas": 3,
            "template": {
                "metadata": {"annotations": {}},
                "spec": {"containers": [
                    {"name": "app", "image": image},
                ]},
            },
        },
    }


class _FakeList:
    def __init__(self, items): self.items = items


class _FakeResource:
    def __init__(self, items, on_patch=None):
        self._items = items
        self.last_call: dict = {}
        self.patches: list[dict] = []
        self._on_patch = on_patch

    def get(self, **kwargs):
        self.last_call = kwargs
        return _FakeList(self._items)

    def patch(self, **kwargs):
        self.patches.append(kwargs)
        # Notify the test fixture so it can record applied YAML the
        # way the old `apply_yaml` mock did. Keeps existing assertions
        # (`_stub_list["get_applied"]()` → list of yaml strings) working.
        if self._on_patch is not None:
            self._on_patch(kwargs.get("body") or {})
        return None


class _FakeDC:
    """Fake DynamicClient that hands out _FakeResource handles per kind.

    `bulk._execute_patches` (post-P2) calls
    `dc.resources.get(api_version, kind).patch(body=...)` — both halves
    need to be mockable. The fixture `_stub_list` mutates
    `state["items"]` and constructs a matching `_FakeResource` to be
    served by `get`.
    """

    def __init__(self, get_resource=None):
        self.resources = self
        self._get_resource = get_resource or (lambda **kw: _FakeResource([]))

    def get(self, **kwargs):
        return self._get_resource(**kwargs)


@pytest.fixture
def _stub_list(monkeypatch):
    """Returns a function to set the next list response + capture apply calls."""
    state = {
        "items": [],
        "applied": [],   # list of yaml strings sent to apply_yaml
    }

    def fake_apply(y):
        state["applied"].append(y)
        # Mimic real apply output: "<kind>/<name> configured"
        try:
            import yaml as _y
            obj = _y.safe_load(y)
            kind = obj.get("kind", "X")
            ns = obj.get("metadata", {}).get("namespace", "")
            name = obj.get("metadata", {}).get("name", "?")
            return f"{kind.lower()}.apps/{name} configured" if ns else f"{kind.lower()}/{name} configured"
        except Exception:
            return "ok"

    def fake_records(y):
        # Mirror the real `_apply_yaml_records` shape so bulk's success
        # counter (`startswith(("created","configured","unchanged"))`) hits.
        fake_apply(y)
        try:
            import yaml as _y
            obj = _y.safe_load(y)
            return [{
                "kind": obj.get("kind", "X"),
                "name": obj.get("metadata", {}).get("name", "?"),
                "namespace": obj.get("metadata", {}).get("namespace"),
                "action": "configured (patched)",
                "error": None,
            }]
        except Exception:
            return [{
                "kind": "X", "name": "?", "namespace": None,
                "action": "configured (patched)", "error": None,
            }]

    def _on_patch(body):
        """Capture patched manifest as YAML the same way the old
        `apply_yaml` mock did — `state["applied"]` stays the canonical
        list of yaml strings passed through the bulk path."""
        import yaml as _y
        state["applied"].append(_y.safe_dump(body))

    monkeypatch.setattr(bulk.generic, "apply_yaml", fake_apply)
    monkeypatch.setattr(bulk.generic, "_apply_yaml_records", fake_records)
    monkeypatch.setattr(
        bulk.generic,
        "_dyn_client",
        lambda: _FakeDC(
            get_resource=lambda **kw: _FakeResource(state["items"], on_patch=_on_patch)
        ),
    )

    def install(items):
        state["items"] = list(items)
        state["applied"] = []
        return _FakeResource(state["items"], on_patch=_on_patch)

    def make_resource(items):
        state["items"] = list(items)
        state["applied"] = []
        r = _FakeResource(state["items"], on_patch=_on_patch)
        monkeypatch.setattr(bulk.generic, "_resource_for_kind", lambda dc, kind: r)
        return r

    def get_applied():
        return state["applied"]

    return {"install": install, "make_resource": make_resource, "get_applied": get_applied}


@pytest.fixture(autouse=True)
def _clear_settings():
    reset_settings_cache()
    yield
    reset_settings_cache()


# ---------- bulk_set_image -------------------------------------------------


def test_bulk_set_image_dry_run_shows_preview_no_token(_stub_list):
    _stub_list["make_resource"]([_deploy("app", "web")])
    out = bulk.bulk_set_image("app=nginx", "app", "nginx:1.26")
    assert "DRY-RUN" in out
    assert "nginx:1.25" in out  # current
    assert "nginx:1.26" in out  # target
    # No actual token issued in dry-run (the literal word appears in the
    # instruction text "Re-call ... confirmation_token", so we look for
    # the HMAC two-segment base64 token instead).
    import re
    assert not re.search(r"[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}", out)
    assert _stub_list["get_applied"]() == []  # no write


def test_bulk_set_image_confirm_false_issues_token(_stub_list):
    _stub_list["make_resource"]([_deploy("app", "web"), _deploy("app", "worker")])
    out = bulk.bulk_set_image("app=nginx", "app", "nginx:1.26",
                              confirm=False, dry_run=False)
    assert "PREVIEW" in out
    assert "confirmation_token" in out
    # The token is a 2-segment base64 string
    import re
    m = re.search(r"confirmation_token[^\n]*\n([A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)", out)
    assert m, f"token not found in output: {out!r}"
    assert _stub_list["get_applied"]() == []  # confirm=False, no apply


def test_bulk_set_image_confirm_true_applies_each(_stub_list):
    _stub_list["make_resource"]([_deploy("app", "web"), _deploy("app", "worker")])
    # 1. Get token
    preview = bulk.bulk_set_image("app=nginx", "app", "nginx:1.26",
                                  confirm=False, dry_run=False)
    import re
    token = re.search(r"confirmation_token[^\n]*\n([A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)",
                      preview).group(1)
    # 2. Confirm
    out = bulk.bulk_set_image("app=nginx", "app", "nginx:1.26",
                              dry_run=False, confirm=True,
                              confirmation_token=token)
    assert "applied to 2/2 resources" in out
    applied = _stub_list["get_applied"]()
    assert len(applied) == 2
    # New image rendered
    for yaml_text in applied:
        assert "nginx:1.26" in yaml_text
        assert "nginx:1.25" not in yaml_text


def test_bulk_set_image_token_mismatch_on_image_rejected(_stub_list):
    """Token issued for image A must not be reusable for image B."""
    _stub_list["make_resource"]([_deploy("app", "web")])
    preview = bulk.bulk_set_image("app=nginx", "app", "nginx:1.26",
                                  confirm=False, dry_run=False)
    import re
    token = re.search(r"confirmation_token[^\n]*\n([A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)",
                      preview).group(1)
    with pytest.raises(TokenError, match="image"):
        bulk.bulk_set_image("app=nginx", "app", "nginx:1.27-DIFFERENT",
                            dry_run=False, confirm=True, confirmation_token=token)


def test_bulk_set_image_token_mismatch_on_container_rejected(_stub_list):
    _stub_list["make_resource"]([_deploy("app", "web")])
    preview = bulk.bulk_set_image("app=nginx", "app", "nginx:1.26",
                                  confirm=False, dry_run=False)
    import re
    token = re.search(r"confirmation_token[^\n]*\n([A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)",
                      preview).group(1)
    with pytest.raises(TokenError, match="container"):
        bulk.bulk_set_image("app=nginx", "different-container", "nginx:1.26",
                            dry_run=False, confirm=True, confirmation_token=token)


def test_bulk_set_image_token_mismatch_on_selector_rejected(_stub_list):
    _stub_list["make_resource"]([_deploy("app", "web")])
    preview = bulk.bulk_set_image("app=nginx", "app", "nginx:1.26",
                                  confirm=False, dry_run=False)
    import re
    token = re.search(r"confirmation_token[^\n]*\n([A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)",
                      preview).group(1)
    with pytest.raises(TokenError, match="label_selector"):
        bulk.bulk_set_image("app=DIFFERENT", "app", "nginx:1.26",
                            dry_run=False, confirm=True, confirmation_token=token)


def test_bulk_set_image_token_scope_excludes_new_resources(_stub_list):
    """If a new resource with the same label appears between preview and
    confirm, the token's matched_names is authoritative — the new one
    is NOT touched. (This is the per-resource safety net.)"""
    # Preview with 1 resource
    _stub_list["make_resource"]([_deploy("app", "web")])
    preview = bulk.bulk_set_image("app=nginx", "app", "nginx:1.26",
                                  confirm=False, dry_run=False)
    import re
    token = re.search(r"confirmation_token[^\n]*\n([A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)",
                      preview).group(1)
    # Confirm: now there are 2 resources with the same label
    _stub_list["make_resource"]([_deploy("app", "web"),
                                 _deploy("app", "newbie")])
    out = bulk.bulk_set_image("app=nginx", "app", "nginx:1.26",
                              dry_run=False, confirm=True, confirmation_token=token)
    applied = _stub_list["get_applied"]()
    names = []
    import yaml as _y
    for y in applied:
        names.append(_y.safe_load(y)["metadata"]["name"])
    # Token only covered "web"; "newbie" was added between preview and
    # confirm → must NOT be in applied.
    assert "web" in names
    assert "newbie" not in names
    assert "applied to 1/1 resources" in out  # the "newbie" wasn't in matched_names at all


def test_bulk_set_image_read_only_rejected(monkeypatch, _stub_list):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "true")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="read-only"):
        bulk.bulk_set_image("app=nginx", "app", "nginx:1.26",
                            dry_run=False, confirm=True, confirmation_token="dummy")


def test_bulk_set_image_allowlist_blocks_other_ns(monkeypatch, _stub_list):
    monkeypatch.setenv("K8S_MCP_NAMESPACE_ALLOWLIST", "app")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="not allowed"):
        bulk.bulk_set_image("app=x", "app", "nginx:1.26", namespace="prod",
                            dry_run=False, confirm=True, confirmation_token="dummy")


def test_bulk_set_image_no_matches_returns_clean_message(_stub_list):
    _stub_list["make_resource"]([])
    out = bulk.bulk_set_image("app=nope", "app", "nginx:1.26")
    assert "Matched 0 resource(s)" in out
    assert "(no resources)" in out


def test_bulk_set_image_container_not_found_returns_helpful_error(_stub_list):
    _stub_list["make_resource"]([_deploy("app", "web")])
    # The dry-run lists the container as "not found"
    out = bulk.bulk_set_image("app=nginx", "ghost-container", "nginx:1.26")
    assert "ghost-container" in out
    assert "app" in out  # the actual container name is shown


# ---------- bulk_restart ---------------------------------------------------


def test_bulk_restart_dry_run(_stub_list):
    _stub_list["make_resource"]([_deploy("app", "web")])
    out = bulk.bulk_restart("app=nginx")
    assert "DRY-RUN" in out
    assert "rolling restart" in out
    assert _stub_list["get_applied"]() == []


def test_bulk_restart_confirm_stamps_annotation(_stub_list):
    _stub_list["make_resource"]([_deploy("app", "web")])
    preview = bulk.bulk_restart("app=nginx", confirm=False, dry_run=False)
    import re
    token = re.search(r"confirmation_token[^\n]*\n([A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)",
                      preview).group(1)
    out = bulk.bulk_restart("app=nginx", dry_run=False,
                            confirm=True, confirmation_token=token)
    assert "applied to 1/1" in out
    applied = _stub_list["get_applied"]()
    import yaml as _y
    manifest = _y.safe_load(applied[0])
    anns = manifest["spec"]["template"]["metadata"]["annotations"]
    assert "kubectl.kubernetes.io/restartedAt" in anns
    # ISO timestamp looks like 2026-07-03T12:34:56Z
    assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
                    anns["kubectl.kubernetes.io/restartedAt"])


# ---------- bulk_scale -----------------------------------------------------


def test_bulk_scale_rejects_daemonset(_stub_list):
    _stub_list["make_resource"]([])
    with pytest.raises(ValueError, match="DaemonSet"):
        bulk.bulk_scale("app=x", replicas=3, kind="DaemonSet")


def test_bulk_scale_rejects_negative_replicas(_stub_list):
    _stub_list["make_resource"]([])
    with pytest.raises(ValueError, match="≥ 0"):
        bulk.bulk_scale("app=x", replicas=-1)


def test_bulk_scale_dry_run_shows_current_and_target(_stub_list):
    _stub_list["make_resource"]([_deploy("app", "web"), _sts("app", "db")])
    out = bulk.bulk_scale("app=nginx", replicas=5)
    assert "DRY-RUN" in out
    # deployment has 2, statefulset has 3
    assert "2" in out
    assert "3" in out
    assert "5" in out  # target


def test_bulk_scale_confirm_patches_replicas(_stub_list):
    _stub_list["make_resource"]([_deploy("app", "web")])
    preview = bulk.bulk_scale("app=nginx", replicas=10, confirm=False, dry_run=False)
    import re
    token = re.search(r"confirmation_token[^\n]*\n([A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)",
                      preview).group(1)
    out = bulk.bulk_scale("app=nginx", replicas=10,
                          dry_run=False, confirm=True, confirmation_token=token)
    assert "applied to 1/1" in out
    import yaml as _y
    manifest = _y.safe_load(_stub_list["get_applied"]()[0])
    assert manifest["spec"]["replicas"] == 10


def test_bulk_scale_token_mismatch_on_replicas(_stub_list):
    _stub_list["make_resource"]([_deploy("app", "web")])
    preview = bulk.bulk_scale("app=nginx", replicas=10, confirm=False, dry_run=False)
    import re
    token = re.search(r"confirmation_token[^\n]*\n([A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)",
                      preview).group(1)
    with pytest.raises(TokenError, match="replicas"):
        bulk.bulk_scale("app=nginx", replicas=20,
                        dry_run=False, confirm=True, confirmation_token=token)


def test_bulk_scale_cross_op_token_rejected(_stub_list):
    """A token issued by bulk_set_image must not unlock bulk_scale."""
    _stub_list["make_resource"]([_deploy("app", "web")])
    preview = bulk.bulk_set_image("app=nginx", "app", "nginx:1.26",
                                  confirm=False, dry_run=False)
    import re
    token = re.search(r"confirmation_token[^\n]*\n([A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)",
                      preview).group(1)
    with pytest.raises(TokenError, match="op="):
        bulk.bulk_scale("app=nginx", replicas=3, dry_run=False,
                        confirm=True, confirmation_token=token)


# ---------- guards --------------------------------------------------------


def test_bulk_set_image_requires_label_selector(_stub_list):
    with pytest.raises(ValueError, match="label_selector"):
        bulk.bulk_set_image("", "app", "nginx:1.26")


def test_bulk_set_image_allowlist_allows_when_namespace_matches(monkeypatch, _stub_list):
    monkeypatch.setenv("K8S_MCP_NAMESPACE_ALLOWLIST", "app")
    reset_settings_cache()
    _stub_list["make_resource"]([_deploy("app", "web")])
    out = bulk.bulk_set_image("app=nginx", "app", "nginx:1.26", namespace="app")
    # Got past the guard, dry-run rendered
    assert "DRY-RUN" in out


def test_bulk_set_image_allowlist_blocks_cluster_scoped_writes(monkeypatch, _stub_list):
    """With allowlist set, namespace=None (cluster-scoped) writes are
    rejected — same policy as `delete_resource`."""
    monkeypatch.setenv("K8S_MCP_NAMESPACE_ALLOWLIST", "app")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="not allowed"):
        bulk.bulk_set_image("app=x", "app", "nginx:1.26", namespace=None,
                            dry_run=False, confirm=True, confirmation_token="dummy")


def test_register_attaches_to_mcp():
    calls: list = []
    class _FakeMCP:
        def tool(self):
            def deco(fn):
                calls.append(fn)
                return fn
            return deco
    bulk.register(_FakeMCP())
    assert bulk.bulk_set_image in calls
    assert bulk.bulk_restart in calls
    assert bulk.bulk_scale in calls
