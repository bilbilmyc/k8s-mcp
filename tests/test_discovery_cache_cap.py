"""Tests for the OpenAPI schema cache in `k8s_mcp.tools.discovery`.

Three production guards live here:
  - TTL: a long-running MCP session that installs a CRD mid-flight sees
    the new type within `_OPENAPI_CACHE_TTL_SECONDS` (300s) without
    paying the fetch cost on every explain_resource call.
  - Size cap: a CRD-heavy cluster's OpenAPI schema can be tens of MiB.
    Pinning that in process memory for the session lifetime is a DoS
    risk; when the freshly-fetched schema exceeds
    `_OPENAPI_CACHE_MAX_BYTES`, the helper returns it but does NOT cache,
    so the next explain_resource call refetches.
  - Latent-bug isolation: the actual apiserver fetch (which went via
    `client.OpenApiApi` in older k8s client versions, removed in v36)
    is split into `_fetch_openapi_spec()` and tested separately in
    `test_discovery_openapi_fetch.py`. Here we mock that seam so the
    cap / TTL logic is independently verifiable.
"""
from __future__ import annotations

import json

import pytest

from k8s_mcp.tools import discovery


@pytest.fixture(autouse=True)
def _reset_cache():
    """Clear the module-level cache between tests so order is irrelevant."""
    discovery.reset_openapi_cache()
    yield
    discovery.reset_openapi_cache()


def _fake_spec(schemas: dict) -> dict:
    """Wrap a schemas dict in the OpenAPI v3 shape `explain_resource` expects."""
    return {"components": {"schemas": schemas}}


def _fake_fetch_with(monkeypatch, spec):
    """Stub `_fetch_openapi_spec` to return `spec` without hitting the apiserver."""
    monkeypatch.setattr(discovery, "_fetch_openapi_spec", lambda: spec)


# ---------- happy path: cache populated when within cap --------------------


def test_small_schema_is_cached(monkeypatch):
    """A modest schema fits inside the cap → cached, second call does not
    re-fetch."""
    call_count = {"n": 0}

    def _counted_fetch():
        call_count["n"] += 1
        return _fake_spec({"Pod": {"type": "object"}})

    monkeypatch.setattr(discovery, "_fetch_openapi_spec", _counted_fetch)

    a = discovery._get_openapi_schema()
    b = discovery._get_openapi_schema()
    assert a == b == {"Pod": {"type": "object"}}
    assert call_count["n"] == 1, "second call within TTL should hit the cache"


# ---------- size cap: oversized schema is not cached -----------------------


def test_oversized_schema_skips_cache_but_still_returns(monkeypatch):
    """A schema over the cap is returned to the caller but NOT retained in
    `discovery._openapi_cache` → next call refetches (defending against a
    CRD-heavy cluster pinning tens of MiB of rarely-touched schemas)."""
    big_value = "x" * (discovery._OPENAPI_CACHE_MAX_BYTES + 1024)
    big_schema = {"Huge": {"type": "object", "description": big_value}}
    assert len(json.dumps(big_schema)) > discovery._OPENAPI_CACHE_MAX_BYTES

    fetch_count = {"n": 0}

    def _counted_fetch():
        fetch_count["n"] += 1
        return _fake_spec(big_schema)

    monkeypatch.setattr(discovery, "_fetch_openapi_spec", _counted_fetch)

    a = discovery._get_openapi_schema()
    # Returned the schema even though we won't cache it.
    assert a == big_schema
    # First call hit the fetcher.
    assert fetch_count["n"] == 1
    # Cache stays empty, so the next call refetches.
    assert discovery._openapi_cache is None
    discovery._get_openapi_schema()
    assert fetch_count["n"] == 2, "oversized schema must not be cached"


def test_size_boundary_at_cap_inclusive(monkeypatch):
    """Schema at-or-below the cap is cached (≤). One byte over → not cached."""
    padding_value = "x" * (discovery._OPENAPI_CACHE_MAX_BYTES - 50)
    tight_schema = {"Pod": {"type": "object", "description": padding_value}}
    serialized_size = len(json.dumps(tight_schema))
    assert serialized_size <= discovery._OPENAPI_CACHE_MAX_BYTES

    fetch_count = {"n": 0}

    def _counted_fetch():
        fetch_count["n"] += 1
        return _fake_spec(tight_schema)

    monkeypatch.setattr(discovery, "_fetch_openapi_spec", _counted_fetch)

    discovery._get_openapi_schema()
    discovery._get_openapi_schema()
    assert fetch_count["n"] == 1, "schema at the cap must be cached"


# ---------- TTL guard ------------------------------------------------------


def test_ttl_expiry_forces_refetch(monkeypatch):
    """Once `_OPENAPI_CACHE_TTL_SECONDS` passes, the next call refetches."""
    fetch_count = {"n": 0}

    def _counted_fetch():
        fetch_count["n"] += 1
        return _fake_spec({"Pod": {"type": "object"}})

    monkeypatch.setattr(discovery, "_fetch_openapi_spec", _counted_fetch)
    fake_now = {"t": 1000.0}

    monkeypatch.setattr(discovery, "_now", lambda: fake_now["t"])

    discovery._get_openapi_schema()
    # Within TTL → cache hit, no refetch.
    fake_now["t"] += discovery._OPENAPI_CACHE_TTL_SECONDS - 1
    discovery._get_openapi_schema()
    assert fetch_count["n"] == 1
    # Past TTL → refetch.
    fake_now["t"] += discovery._OPENAPI_CACHE_TTL_SECONDS + 1
    discovery._get_openapi_schema()
    assert fetch_count["n"] == 2


# ---------- helpers (size cap policy in isolation) -------------------------


def test_store_openapi_spec_if_within_cap_within():
    """The size-cap policy helper accepts a small spec and records it."""
    spec = _fake_spec({"Pod": {"type": "object"}})
    out = discovery._store_openapi_spec_if_within_cap(spec)
    assert out == {"Pod": {"type": "object"}}
    assert discovery._openapi_cache == {"Pod": {"type": "object"}}
    assert discovery._openapi_cache_at > 0


def test_store_openapi_spec_if_within_cap_over():
    """The size-cap policy helper refuses an oversized spec; cache stays empty."""
    big_value = "x" * (discovery._OPENAPI_CACHE_MAX_BYTES + 1)
    spec = _fake_spec({"Big": {"type": "object", "description": big_value}})
    out = discovery._store_openapi_spec_if_within_cap(spec)
    assert out == {"Big": {"type": "object", "description": big_value}}
    assert discovery._openapi_cache is None
    assert discovery._openapi_cache_at == 0.0


# ---------- reset helper ---------------------------------------------------


def test_reset_openapi_cache_clears_state():
    """The test helper clears both the cached dict and the TTL clock;
    a stale-schema scenario relies on this for isolation."""
    discovery._store_openapi_spec_if_within_cap(
        _fake_spec({"K": {"type": "object"}})
    )
    assert discovery._openapi_cache is not None
    discovery.reset_openapi_cache()
    assert discovery._openapi_cache is None
    assert discovery._openapi_cache_at == 0.0


# ---------- non-dict spec doesn't crash ------------------------------------


def test_non_dict_spec_returns_empty_cache(monkeypatch):
    """Real apiservers always return a dict, but a misbehaving stub / mock
    shouldn't take down explain_resource. The helper returns {} and does
    not crash; the cap-size check (len(json.dumps)) is fine on {}=2 bytes."""
    monkeypatch.setattr(discovery, "_fetch_openapi_spec", lambda: "not a dict")
    out = discovery._get_openapi_schema()
    assert out == {}
