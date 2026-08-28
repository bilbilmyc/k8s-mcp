"""Tests for shared Kubernetes client transport and discovery caching."""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from kubernetes import client

from k8s_mcp import client as client_mod
from k8s_mcp.config import Settings


def _settings(**overrides) -> Settings:
    values = {
        "api_server": "https://cluster.example",
        "api_token": "super-secret-token",
    }
    values.update(overrides)
    return Settings(**values)


def test_bounded_api_client_injects_real_request_timeout():
    api = object.__new__(client_mod._BoundedApiClient)
    with patch.object(client.ApiClient, "request", return_value="ok") as request:
        assert api.request("GET", "https://cluster.example/version") == "ok"
    assert request.call_args.kwargs["_request_timeout"] == (5, 30)


def test_bounded_api_client_preserves_explicit_request_timeout():
    api = object.__new__(client_mod._BoundedApiClient)
    with patch.object(client.ApiClient, "request", return_value="ok") as request:
        api.request(
            "GET",
            "https://cluster.example/version",
            _request_timeout=(1, 2),
        )
    assert request.call_args.kwargs["_request_timeout"] == (1, 2)


def test_api_client_is_reused_and_log_does_not_expose_token(caplog):
    settings = _settings(max_concurrent_tools=5)
    configuration = client.Configuration(host=settings.api_server)
    caplog.set_level(logging.DEBUG, logger="k8s_mcp.client")

    with patch.object(client_mod, "load_configuration", return_value=configuration) as load:
        first = client_mod.get_api_client(settings)
        second = client_mod.get_api_client(settings)

    assert first is second
    assert first.configuration.connection_pool_maxsize == 10
    load.assert_called_once_with(settings)
    assert settings.api_token not in caplog.text


def test_concurrent_first_use_builds_one_api_client():
    settings = _settings()
    configuration = client.Configuration(host=settings.api_server)
    with patch.object(client_mod, "load_configuration", return_value=configuration) as load:
        with ThreadPoolExecutor(max_workers=8) as executor:
            clients = list(executor.map(lambda _: client_mod.get_api_client(settings), range(32)))

    assert len({id(api) for api in clients}) == 1
    load.assert_called_once_with(settings)


def test_api_client_key_rotation_closes_previous_client():
    first = MagicMock()
    second = MagicMock()
    configurations = [
        client.Configuration(host="https://cluster-a.example"),
        client.Configuration(host="https://cluster-b.example"),
    ]
    with patch.object(client_mod, "load_configuration", side_effect=configurations), \
         patch.object(client_mod, "_BoundedApiClient", side_effect=[first, second]):
        assert client_mod.get_api_client(_settings(api_server="https://cluster-a.example")) is first
        assert client_mod.get_api_client(_settings(api_server="https://cluster-b.example")) is second

    first.rest_client.pool_manager.clear.assert_called_once_with()
    first.close.assert_called_once_with()


def test_standard_kubeconfig_path_participates_in_client_identity(monkeypatch):
    settings = _settings(api_server=None, api_token=None)
    monkeypatch.setenv("KUBECONFIG", "cluster-a.yaml")
    first = client_mod.client_cache_key(settings)
    monkeypatch.setenv("KUBECONFIG", "cluster-b.yaml")
    assert client_mod.client_cache_key(settings) != first


def test_dynamic_client_is_reused_for_same_settings():
    settings = _settings()
    api_client = object()
    dynamic_client = MagicMock()
    with patch.object(client_mod, "get_api_client", return_value=api_client), \
         patch("kubernetes.dynamic.DynamicClient", return_value=dynamic_client) as build:
        first = client_mod.get_dynamic_client(settings)
        second = client_mod.get_dynamic_client(settings)

    assert first is second
    assert first._dynamic_client is dynamic_client
    build.assert_called_once_with(api_client)


def test_dynamic_client_construction_does_not_block_cached_api_client():
    settings = _settings()
    configuration = client.Configuration(host=settings.api_server)
    entered = threading.Event()
    release = threading.Event()

    def slow_dynamic(api_client):
        entered.set()
        assert release.wait(timeout=2)
        result = MagicMock()
        result.api_client = api_client
        return result

    with patch.object(client_mod, "load_configuration", return_value=configuration), \
         patch("kubernetes.dynamic.DynamicClient", side_effect=slow_dynamic):
        api_client = client_mod.get_api_client(settings)
        with ThreadPoolExecutor(max_workers=2) as executor:
            dynamic_future = executor.submit(client_mod.get_dynamic_client, settings)
            assert entered.wait(timeout=1)
            api_future = executor.submit(client_mod.get_api_client, settings)
            assert api_future.result(timeout=0.2) is api_client
            release.set()
            assert dynamic_future.result(timeout=1).api_client is api_client


def test_shared_dynamic_discovery_is_serialized():
    state = {"active": 0, "max_active": 0}

    class _Discoverer:
        def search(self, **_kwargs):
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            time.sleep(0.005)
            state["active"] -= 1
            return []

    discoverer = client_mod._LockedDiscoverer(_Discoverer())
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: discoverer.search(), range(24)))

    assert state["max_active"] == 1


def test_transport_defaults_follow_tool_concurrency():
    assert client_mod.transport_defaults(_settings(max_concurrent_tools=3)) == {
        "connect_timeout_s": 5,
        "read_timeout_s": 30,
        "connection_pool_size": 8,
    }
    assert client_mod.transport_defaults(_settings(max_concurrent_tools=12))[
        "connection_pool_size"
    ] == 24
