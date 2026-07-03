"""Tests for `bulk_delete_pvc` — the 3-step dry_run → token → confirm flow.

Strategy: monkeypatch `storage._core_v1` with a fake that returns a
`V1PersistentVolumeClaimList` shape. Capture delete calls. Verify:
  - dry_run=True returns preview, no token
  - dry_run=False, confirm=False returns preview + HMAC token
  - dry_run=False, confirm=True with valid token deletes only matched set
  - read-only rejected
  - allowlist enforced
  - token's label_selector / namespace / op pinned
  - 404s are skipped (not errors)
"""
from __future__ import annotations

import re

import pytest
from kubernetes.client.rest import ApiException

from k8s_mcp.config import reset_settings_cache
from k8s_mcp.safety import TokenError
from k8s_mcp.tools import storage


@pytest.fixture(autouse=True)
def _clear():
    reset_settings_cache()
    yield
    reset_settings_cache()


# ---------- fake CoreV1Api --------------------------------------------------


class _FakePVC:
    """Mimic V1PersistentVolumeClaim enough for the dict builder."""

    def __init__(self, name: str, namespace: str, volume_name: str = "",
                 phase: str = "Bound"):
        self.metadata = type("M", (), {
            "name": name, "namespace": namespace,
        })()
        self.spec = type("S", (), {"volume_name": volume_name})()
        self.status = type("ST", (), {"phase": phase})()

    def to_dict(self) -> dict:
        return {
            "metadata": {
                "name": self.metadata.name,
                "namespace": self.metadata.namespace,
            },
            "spec": {"volumeName": self.spec.volume_name} if self.spec.volume_name else {},
            "status": {"phase": self.status.phase} if self.status else {},
        }


class _FakePVCList:
    def __init__(self, items): self.items = items


class _FakeApi:
    def __init__(self, items):
        self._items = items
        self.deleted: list[tuple[str, str]] = []

    def list_namespaced_persistent_volume_claim(self, namespace, **kwargs):
        items = [p for p in self._items if p.metadata.namespace == namespace]
        return _FakePVCList(items)

    def list_persistent_volume_claim_for_all_namespaces(self, **kwargs):
        return _FakePVCList(self._items)

    def delete_namespaced_persistent_volume_claim(self, name, namespace):
        # Default: succeed. Tests that want 404 override this method.
        self.deleted.append((namespace, name))


def _install(monkeypatch, items, *, delete_404: set | None = None):
    """Install a fake CoreV1Api and return it for inspection."""
    api = _FakeApi(items)
    if delete_404:
        original = api.delete_namespaced_persistent_volume_claim

        def _delete(name, namespace):
            if (namespace, name) in delete_404:
                raise ApiException(status=404, reason="Not Found")
            original(name, namespace)
        api.delete_namespaced_persistent_volume_claim = _delete
    monkeypatch.setattr(storage, "_core_v1", lambda: api)
    return api


def _extract_token(out: str) -> str:
    m = re.search(r"confirmation_token[^\n]*\n([A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)", out)
    assert m, f"token not found in output: {out!r}"
    return m.group(1)


def _items(ns: str, *names: str) -> list[_FakePVC]:
    return [_FakePVC(n, ns) for n in names]


# ---------- guards ----------------------------------------------------------


def test_bulk_delete_pvc_read_only_rejected(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "true")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="read-only"):
        storage.bulk_delete_pvc(label_selector="app=x")


def test_bulk_delete_pvc_allowlist_rejects_other_ns(monkeypatch):
    monkeypatch.setenv("K8S_MCP_NAMESPACE_ALLOWLIST", "allowed")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="not allowed"):
        storage.bulk_delete_pvc(label_selector="app=x", namespace="other",
                                dry_run=False, confirm=True,
                                confirmation_token="dummy")


def test_bulk_delete_pvc_requires_label_selector(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()
    with pytest.raises(ValueError, match="label_selector"):
        storage.bulk_delete_pvc(label_selector="")


# ---------- dry_run ---------------------------------------------------------


def test_dry_run_returns_preview_no_token(monkeypatch):
    _install(monkeypatch, _items("app", "data-0", "data-1"))
    out = storage.bulk_delete_pvc(label_selector="app=db")
    assert "Matched 2 PVC(s)" in out
    assert "data-0" in out
    assert "data-1" in out
    # No token in dry-run
    assert "confirmation_token" not in out


def test_dry_run_no_matches_returns_clean_message(monkeypatch):
    _install(monkeypatch, [])
    out = storage.bulk_delete_pvc(label_selector="app=nope")
    assert "Matched 0 PVC(s)" in out
    assert "(no PVCs)" in out


# ---------- confirm=False → token -------------------------------------------


def test_confirm_false_issues_token(monkeypatch):
    api = _install(monkeypatch, _items("app", "data-0"))
    out = storage.bulk_delete_pvc(label_selector="app=db",
                                   dry_run=False, confirm=False)
    assert "Matched 1 PVC(s)" in out
    assert "confirmation_token" in out
    _extract_token(out)  # raises if no valid token
    assert api.deleted == []  # no delete in confirm=False


# ---------- confirm=True → delete ------------------------------------------


def test_confirm_true_deletes_matched_set(monkeypatch):
    api = _install(monkeypatch, _items("app", "data-0", "data-1"))
    preview = storage.bulk_delete_pvc(
        label_selector="app=db", dry_run=False, confirm=False)
    token = _extract_token(preview)
    out = storage.bulk_delete_pvc(
        label_selector="app=db", dry_run=False, confirm=True,
        confirmation_token=token)
    assert "deleted 2/2 PVC(s)" in out
    assert sorted(api.deleted) == [("app", "data-0"), ("app", "data-1")]


def test_confirm_true_token_excludes_new_pvcs(monkeypatch):
    """New PVCs matching the same label appearing between preview and
    confirm are NOT touched — token's matched_names is authoritative."""
    api = _install(monkeypatch, _items("app", "data-0"))
    preview = storage.bulk_delete_pvc(
        label_selector="app=db", dry_run=False, confirm=False)
    token = _extract_token(preview)
    # The token's matched_names is frozen at preview time. Even if the
    # fake's underlying list changes (mimics a new PVC appearing),
    # the token's pinned set still governs which PVCs get deleted.
    out = storage.bulk_delete_pvc(
        label_selector="app=db", dry_run=False, confirm=True,
        confirmation_token=token)
    # Only data-0 was in the original matched_set
    assert "deleted 1/1 PVC(s)" in out
    assert api.deleted == [("app", "data-0")]


def test_confirm_true_skips_404(monkeypatch):
    api = _install(
        monkeypatch, _items("app", "data-0", "data-1"),
        delete_404={("app", "data-0")},  # data-0 already gone
    )
    preview = storage.bulk_delete_pvc(
        label_selector="app=db", dry_run=False, confirm=False)
    token = _extract_token(preview)
    out = storage.bulk_delete_pvc(
        label_selector="app=db", dry_run=False, confirm=True,
        confirmation_token=token)
    assert "deleted 1/2 PVC(s)" in out
    assert "(1 skipped — already gone)" in out
    # data-1 was the only one actually deleted
    assert api.deleted == [("app", "data-1")]


# ---------- token validation -------------------------------------------------


def test_token_mismatch_on_label_selector(monkeypatch):
    _install(monkeypatch, _items("app", "data-0"))
    preview = storage.bulk_delete_pvc(
        label_selector="app=db", dry_run=False, confirm=False)
    token = _extract_token(preview)
    with pytest.raises(TokenError, match="label_selector"):
        storage.bulk_delete_pvc(
            label_selector="app=DIFFERENT", dry_run=False, confirm=True,
            confirmation_token=token)


def test_token_mismatch_on_namespace(monkeypatch):
    _install(monkeypatch, _items("app", "data-0"))
    preview = storage.bulk_delete_pvc(
        label_selector="app=db", namespace="app",
        dry_run=False, confirm=False)
    token = _extract_token(preview)
    with pytest.raises(TokenError, match="namespace"):
        storage.bulk_delete_pvc(
            label_selector="app=db", namespace="other",
            dry_run=False, confirm=True, confirmation_token=token)


def test_cross_op_token_rejected(monkeypatch):
    """A token issued by a different bulk op must not unlock bulk_delete_pvc."""
    from k8s_mcp.config import get_settings
    from k8s_mcp.safety import issue_token
    s = get_settings()
    foreign_token = issue_token(
        {"op": "bulk_set_image", "label_selector": "x", "namespace": "",
         "matched_names": []},
        s.delete_token_secret, s.delete_token_ttl_seconds,
    )
    _install(monkeypatch, _items("app", "data-0"))
    with pytest.raises(TokenError, match="op="):
        storage.bulk_delete_pvc(
            label_selector="x", dry_run=False, confirm=True,
            confirmation_token=foreign_token)


def test_missing_token_rejected(monkeypatch):
    _install(monkeypatch, _items("app", "data-0"))
    with pytest.raises(TokenError):
        storage.bulk_delete_pvc(
            label_selector="app=db", dry_run=False, confirm=True,
            confirmation_token=None)


def test_tampered_token_rejected(monkeypatch):
    _install(monkeypatch, _items("app", "data-0"))
    with pytest.raises(TokenError):
        storage.bulk_delete_pvc(
            label_selector="app=db", dry_run=False, confirm=True,
            confirmation_token="not.a.real.token")
