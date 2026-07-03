"""Tests for `whoami` and `cluster_info`.

Strategy: monkeypatch `get_api_client` to return a fake with attributes
matching what the tools look up, then verify the rendered report.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from kubernetes.client.rest import ApiException

from k8s_mcp.tools import cluster_info, rbac

# ---------- whoami ----------------------------------------------------------


def _subject_review_status(username: str = "alice", uid: str = "u-1",
                           groups: list[str] | None = None) -> MagicMock:
    status = MagicMock()
    status.username = username
    status.uid = uid
    status.groups = groups if groups is not None else ["system:authenticated"]
    return status


def _rules_review_status(rules: list[dict], non_resource: list[str] | None = None):
    status = MagicMock()
    rs = []
    for r in rules:
        rule = MagicMock()
        rule.api_groups = r.get("apiGroups", [""])
        rule.resources = r.get("resources", [])
        rule.verbs = r.get("verbs", [])
        rs.append(rule)
    status.resource_rules = rs
    status.non_resource_rules = non_resource or []
    return status


def test_whoami_renders_identity(monkeypatch):
    captured: dict = {}

    class _Authn:
        def create_self_subject_review(self, body):
            captured["identity_called"] = True
            ret = MagicMock()
            ret.status = _subject_review_status(
                username="system:serviceaccount:app:web",
                uid="sa-uid-1",
                groups=["system:serviceaccounts", "system:authenticated"],
            )
            return ret

    class _Authz:
        def create_self_subject_rules_review(self, body):
            captured["ns"] = body.spec.namespace
            ret = MagicMock()
            ret.status = _rules_review_status([
                {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]},
            ])
            return ret

    api = MagicMock()
    monkeypatch.setattr(rbac, "get_api_client", lambda: api)
    monkeypatch.setattr(rbac.client, "AuthenticationV1Api", lambda c: _Authn())
    monkeypatch.setattr(rbac.client, "AuthorizationV1Api", lambda c: _Authz())

    out = rbac.whoami(namespace="app")
    assert "system:serviceaccount:app:web" in out
    assert "sa-uid-1" in out
    assert "system:serviceaccounts" in out
    assert "pods" in out
    assert "get, list" in out
    assert captured["ns"] == "app"


def test_whoami_identity_failure_returns_error(monkeypatch):
    class _Authn:
        def create_self_subject_review(self, body):
            raise ApiException(status=403, reason="Forbidden")

    class _Authz:
        def create_self_subject_rules_review(self, body):
            raise AssertionError("should not be reached")

    api = MagicMock()
    monkeypatch.setattr(rbac, "get_api_client", lambda: api)
    monkeypatch.setattr(rbac.client, "AuthenticationV1Api", lambda c: _Authn())
    monkeypatch.setattr(rbac.client, "AuthorizationV1Api", lambda c: _Authz())

    out = rbac.whoami()
    assert "whoami" in out
    assert "403" in out
    assert "Forbidden" in out


def test_whoami_no_rules(monkeypatch):
    class _Authn:
        def create_self_subject_review(self, body):
            ret = MagicMock()
            ret.status = _subject_review_status(uid="")
            return ret

    class _Authz:
        def create_self_subject_rules_review(self, body):
            ret = MagicMock()
            ret.status = _rules_review_status([])
            return ret

    api = MagicMock()
    monkeypatch.setattr(rbac, "get_api_client", lambda: api)
    monkeypatch.setattr(rbac.client, "AuthenticationV1Api", lambda c: _Authn())
    monkeypatch.setattr(rbac.client, "AuthorizationV1Api", lambda c: _Authz())

    out = rbac.whoami(namespace="locked")
    assert "no namespace-scoped rules" in out


def test_whoami_rules_review_failure_surfaces(monkeypatch):
    class _Authn:
        def create_self_subject_review(self, body):
            ret = MagicMock()
            ret.status = _subject_review_status()
            return ret

    class _Authz:
        def create_self_subject_rules_review(self, body):
            raise ApiException(status=500, reason="boom")

    api = MagicMock()
    monkeypatch.setattr(rbac, "get_api_client", lambda: api)
    monkeypatch.setattr(rbac.client, "AuthenticationV1Api", lambda c: _Authn())
    monkeypatch.setattr(rbac.client, "AuthorizationV1Api", lambda c: _Authz())

    out = rbac.whoami()
    assert "SelfSubjectRulesReview failed" in out
    assert "500" in out


# ---------- cluster_info ----------------------------------------------------


def test_cluster_info_renders_full_report(monkeypatch):
    cfg = MagicMock()
    cfg.host = "https://api.example.com:6443"
    cfg.api_key = {"Bearer": "redacted"}

    api_client = MagicMock()
    api_client.configuration = cfg

    class _Version:
        git_version = "v1.31.11"
        git_commit = "deadbeef1234567890abcdef"
        major = "1"
        minor = "31"
        platform = "linux/amd64"

    class _VersionApi:
        def __init__(self, c): pass
        def get_code(self): return _Version()

    class _List:
        def __init__(self, items): self.items = items

    class _Core:
        def __init__(self, c): pass
        def list_node(self):
            return _List([{"metadata": {"name": "n1"}}, {"metadata": {"name": "n2"}}])
        def list_namespace(self):
            return _List([{"metadata": {"name": "default"}}, {"metadata": {"name": "kube-system"}}, {"metadata": {"name": "app"}}])
        def list_pod_for_all_namespaces(self):
            return _List([{"metadata": {"name": f"p{i}"}} for i in range(7)])
        def list_service_for_all_namespaces(self):
            return _List([{"metadata": {"name": f"s{i}"}} for i in range(3)])

    class _Apps:
        def __init__(self, c): pass
        def list_deployment_for_all_namespaces(self):
            return _List([{"metadata": {"name": f"d{i}"}} for i in range(2)])

    monkeypatch.setattr(cluster_info, "get_api_client", lambda: api_client)
    monkeypatch.setattr(cluster_info.client, "VersionApi", _VersionApi)
    monkeypatch.setattr(cluster_info.client, "CoreV1Api", _Core)
    monkeypatch.setattr(cluster_info.client, "AppsV1Api", _Apps)

    out = cluster_info.cluster_info()
    assert "api.example.com" in out
    assert "v1.31.11" in out
    assert "linux/amd64" in out
    assert "1.31" in out
    assert "Nodes:        2" in out
    assert "Namespaces:   3" in out
    assert "Pods:         7" in out
    assert "Services:     3" in out
    assert "Deployments:  2" in out
    assert "yes" in out  # bearer token present


def test_cluster_info_no_bearer_token(monkeypatch):
    cfg = MagicMock()
    cfg.host = "https://api.example.com"
    cfg.api_key = {}  # no bearer

    api_client = MagicMock()
    api_client.configuration = cfg

    class _Version:
        git_version = "v1.30.0"
        git_commit = ""
        major = "1"
        minor = "30"
        platform = "linux/amd64"

    class _V:
        def get_code(self): return _Version()
    class _Core:
        def list_node(self): return MagicMock(items=[])
        def list_namespace(self): return MagicMock(items=[])
        def list_pod_for_all_namespaces(self): return MagicMock(items=[])
        def list_service_for_all_namespaces(self): return MagicMock(items=[])
    class _Apps:
        def list_deployment_for_all_namespaces(self): return MagicMock(items=[])

    monkeypatch.setattr(cluster_info, "get_api_client", lambda: api_client)
    monkeypatch.setattr(cluster_info.client, "VersionApi", lambda c: _V())
    monkeypatch.setattr(cluster_info.client, "CoreV1Api", lambda c: _Core())
    monkeypatch.setattr(cluster_info.client, "AppsV1Api", lambda c: _Apps())

    out = cluster_info.cluster_info()
    assert "Bearer token:  no" in out


def test_cluster_info_counts_section_does_not_blank_on_partial_failure(monkeypatch):
    """A single failing section should not blank the rest of the report."""
    cfg = MagicMock()
    cfg.host = "https://api.example.com"
    cfg.api_key = {"Bearer": "x"}

    api_client = MagicMock()
    api_client.configuration = cfg

    class _Version:
        git_version = "v1.31.0"
        git_commit = "x"
        major = "1"
        minor = "31"
        platform = "linux/amd64"

    class _V:
        def get_code(self): return _Version()
    class _Core:
        def list_node(self): return MagicMock(items=[])
        def list_namespace(self): return MagicMock(items=[])
        def list_pod_for_all_namespaces(self):
            raise ApiException(status=403, reason="Forbidden")
        def list_service_for_all_namespaces(self): return MagicMock(items=[])
    class _Apps:
        def list_deployment_for_all_namespaces(self): return MagicMock(items=[])

    monkeypatch.setattr(cluster_info, "get_api_client", lambda: api_client)
    monkeypatch.setattr(cluster_info.client, "VersionApi", lambda c: _V())
    monkeypatch.setattr(cluster_info.client, "CoreV1Api", lambda c: _Core())
    monkeypatch.setattr(cluster_info.client, "AppsV1Api", lambda c: _Apps())

    out = cluster_info.cluster_info()
    assert "Pods:         error: 403 Forbidden" in out
    # Other sections still render
    assert "Nodes:        0" in out
    assert "Services:     0" in out
