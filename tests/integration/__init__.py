"""Integration tests against a live Kubernetes cluster.

These tests are NOT run by the default `pytest` invocation. They require
a real cluster reachable from the test runner (kind / k3d / minikube / a
remote dev cluster) AND the k8s-mcp server to be configured against it.

Run with:

    pytest tests/integration -v

Or skip with:

    pytest --ignore=tests/integration -q

What these tests verify that the unit tests cannot:

  - End-to-end tool calls (apply → get → describe → delete) against real
    apiserver semantics: validation, resourceVersion conflict, namespace
    allowlist enforcement at the apiserver side.
  - RBAC permission errors surface as `Forbidden` rather than crashing.
  - The two-step HMAC delete actually deletes the resource on confirm.
  - Bulk operations honor the `matched_names` snapshot (no TOCTOU).
  - Prometheus discovery returns a working service when one exists, and
    degrades gracefully when none does.

Tests should be self-cleaning — every resource they create must be
deleted in a finally block. Use unique names (uuid suffix) to avoid
collisions across runs and across parallel test runs.
"""
