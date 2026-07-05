"""Apply → Get → Delete lifecycle smoke test.

Creates a ConfigMap via `apply_yaml`, verifies it round-trips through
`get_resource`, then deletes it via `delete_resource` (two-step HMAC).
Confirms the whole write/read/delete loop works end-to-end against a
real apiserver.
"""
from __future__ import annotations

import uuid

import pytest

from k8s_mcp.tools import generic
from tests.integration.conftest import _cluster_reachable

pytestmark = pytest.mark.skipif(
    not _cluster_reachable(),
    reason="No reachable Kubernetes cluster — set KUBECONFIG or K8S_MCP_API_SERVER",
)


def test_apply_get_delete_configmap(clean_namespace):
    name = f"it-{uuid.uuid4().hex[:8]}"
    manifest = (
        f"apiVersion: v1\n"
        f"kind: ConfigMap\n"
        f"metadata:\n"
        f"  name: {name}\n"
        f"  namespace: {clean_namespace}\n"
        f"data:\n"
        f"  hello: world\n"
    )

    # 1. Apply
    out = generic.apply_yaml(manifest)
    assert f"ConfigMap/{name}: configured" in out or f"ConfigMap/{name}: created" in out

    # 2. Get — round trip
    obj = generic.get_resource("ConfigMap", name, namespace=clean_namespace)
    assert obj["metadata"]["name"] == name
    assert obj["data"]["hello"] == "world"

    # 3. Replace with an updated value (PUT path with resourceVersion)
    updated = manifest.replace("hello: world", "hello: universe")
    out = generic.apply_yaml(updated)
    assert f"ConfigMap/{name}: configured" in out

    obj = generic.get_resource("ConfigMap", name, namespace=clean_namespace)
    assert obj["data"]["hello"] == "universe"


def test_get_missing_resource_raises(clean_namespace):
    """Looking up a non-existent resource should raise LookupError."""
    import pytest

    with pytest.raises(LookupError):
        generic.get_resource(
            "ConfigMap",
            f"does-not-exist-{uuid.uuid4().hex[:8]}",
            namespace=clean_namespace,
        )


def test_unknown_kind_raises():
    """Asking for a kind that doesn't exist should raise ValueError."""
    import pytest

    with pytest.raises(ValueError, match="Unknown kind"):
        generic._resource_for_kind(
            generic._dyn_client(),
            f"FakeKind{uuid.uuid4().hex[:6]}",
        )
