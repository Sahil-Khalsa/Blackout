"""Live tests for ToxiproxyClient -- self-skips if localhost:8474 isn't
reachable, same convention as test_ollama_integration.py. Start Toxiproxy
first with `docker compose up -d`."""

import pytest

from blackout_chaos.toxiproxy_client import ToxiproxyClient, toxiproxy_reachable

pytestmark = pytest.mark.skipif(not toxiproxy_reachable(), reason="Toxiproxy not reachable")


@pytest.fixture
def client():
    c = ToxiproxyClient()
    c.reset()
    yield c
    c.reset()


def test_create_and_delete_proxy(client):
    client.create_proxy("chaos_test_proxy", listen="0.0.0.0:29999", upstream="host.docker.internal:29998")
    client.delete_proxy("chaos_test_proxy")


def test_add_and_remove_toxic(client):
    client.create_proxy("chaos_test_proxy", listen="0.0.0.0:29999", upstream="host.docker.internal:29998")
    try:
        client.add_toxic("chaos_test_proxy", name="latency_down", type="latency", stream="downstream", latency=100)
        client.remove_toxic("chaos_test_proxy", "latency_down")
    finally:
        client.delete_proxy("chaos_test_proxy")


def test_set_enabled_toggles_proxy(client):
    client.create_proxy("chaos_test_proxy", listen="0.0.0.0:29999", upstream="host.docker.internal:29998")
    try:
        client.set_enabled("chaos_test_proxy", False)
        client.set_enabled("chaos_test_proxy", True)
    finally:
        client.delete_proxy("chaos_test_proxy")


def test_reset_clears_toxics_without_deleting_the_proxy(client):
    client.create_proxy("chaos_test_proxy", listen="0.0.0.0:29999", upstream="host.docker.internal:29998")
    try:
        client.add_toxic("chaos_test_proxy", name="latency_down", type="latency", stream="downstream", latency=100)
        client.reset()
        # if reset left the old toxic in place, re-adding the same name fails
        client.add_toxic("chaos_test_proxy", name="latency_down", type="latency", stream="downstream", latency=50)
    finally:
        client.delete_proxy("chaos_test_proxy")
