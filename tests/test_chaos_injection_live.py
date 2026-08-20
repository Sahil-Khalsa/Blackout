"""Live network-toxic tests for injection.py -- split out of
test_chaos_injection.py so `import requests` (needed only here, not a
transitive dependency of the `dev`/`cloud` extras) can't block collection of
the pure disk-exhaustion tests. Self-skips per-function if localhost:8474
isn't reachable, same convention as test_ollama_integration.py. Start
Toxiproxy first with `docker compose up -d`."""

import pytest

import requests

from blackout_chaos.injection import (
    flapping,
    mid_plan,
    partial_response,
    post_request_pre_response,
    pre_plan,
    recovery_storm,
    slow_success,
)
from blackout_chaos.mock_backend import MockBackendServer
from blackout_chaos.toxiproxy_client import ToxiproxyClient, toxiproxy_reachable

_LIVE = pytest.mark.skipif(not toxiproxy_reachable(), reason="Toxiproxy not reachable")


@pytest.fixture
def proxy():
    client = ToxiproxyClient()
    client.reset()
    server = MockBackendServer()
    server.start()
    client.create_proxy(
        "chaos_injection_test", listen="0.0.0.0:20001", upstream=f"host.docker.internal:{server.port}"
    )
    yield client, "chaos_injection_test"
    client.delete_proxy("chaos_injection_test")
    server.stop()


def _proxy_state(client, name):
    resp = requests.get(f"{client.base_url}/proxies/{name}", timeout=5.0)
    resp.raise_for_status()
    return resp.json()


def _toxics(client, name):
    resp = requests.get(f"{client.base_url}/proxies/{name}/toxics", timeout=5.0)
    resp.raise_for_status()
    return {t["name"] for t in resp.json()}


@_LIVE
def test_pre_plan_disables_and_restores_the_proxy(proxy):
    client, name = proxy
    with pre_plan(client, name):
        assert _proxy_state(client, name)["enabled"] is False
    assert _proxy_state(client, name)["enabled"] is True


@_LIVE
def test_mid_plan_adds_and_removes_a_limit_data_toxic(proxy):
    client, name = proxy
    with mid_plan(client, name):
        assert "chaos_toxic" in _toxics(client, name)
    assert "chaos_toxic" not in _toxics(client, name)


@_LIVE
def test_post_request_pre_response_adds_and_removes_a_timeout_toxic(proxy):
    client, name = proxy
    with post_request_pre_response(client, name):
        assert "chaos_toxic" in _toxics(client, name)
    assert "chaos_toxic" not in _toxics(client, name)


@_LIVE
def test_partial_response_adds_and_removes_a_limit_data_toxic(proxy):
    client, name = proxy
    with partial_response(client, name):
        assert "chaos_toxic" in _toxics(client, name)
    assert "chaos_toxic" not in _toxics(client, name)


@_LIVE
def test_slow_success_adds_and_removes_a_latency_toxic(proxy):
    client, name = proxy
    with slow_success(client, name, latency_ms=100):
        assert "chaos_toxic" in _toxics(client, name)
    assert "chaos_toxic" not in _toxics(client, name)


@_LIVE
def test_flapping_ends_with_the_proxy_enabled(proxy):
    client, name = proxy
    with flapping(client, name, cycles=2, interval_s=0.05):
        pass
    assert _proxy_state(client, name)["enabled"] is True


@_LIVE
def test_recovery_storm_disables_during_and_restores_after(proxy):
    client, name = proxy
    with recovery_storm(client, name):
        assert _proxy_state(client, name)["enabled"] is False
    assert _proxy_state(client, name)["enabled"] is True
