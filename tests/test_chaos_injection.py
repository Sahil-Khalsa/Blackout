"""Tests for injection.py. Disk-exhaustion is pure (no Docker, runs
unconditionally); the 7 network injectors need live Toxiproxy and self-skip
per-function, same convention as test_ollama_integration.py."""

import pytest

from blackout_core import (
    AgentLoop,
    Effect,
    Intent,
    IntentJournal,
    JournalUnavailable,
    ModelRouter,
    OfflinePolicy,
    PolicyEngine,
    Rule,
    RulesBackend,
    Tier,
    TierResolver,
    ToolRegistry,
)

from blackout_chaos.injection import disk_exhausted


@pytest.fixture
def journal(tmp_path):
    j = IntentJournal(tmp_path / "journal.db")
    yield j
    j.close()


def _intent() -> Intent:
    return Intent.from_evaluation(
        tool="place_restock_order",
        args={"sku": "SKU-991", "qty": 1, "window": "w1"},
        idempotency_key="restock:SKU-991:w1",
        tier_at_creation=int(Tier.LOCAL),
        ttl_seconds=3600,
    )


def test_append_raises_journal_unavailable_inside_disk_exhausted(journal):
    with disk_exhausted(journal):
        with pytest.raises(JournalUnavailable):
            journal.append(_intent())
    assert journal.pending() == []


def test_journal_recovers_after_disk_exhausted_exits(journal):
    with disk_exhausted(journal):
        with pytest.raises(JournalUnavailable):
            journal.append(_intent())

    journal.append(_intent())
    assert len(journal.pending()) == 1


def test_agent_loop_step_demotes_to_journal_down_under_disk_exhaustion(journal):
    registry = ToolRegistry()

    @registry.tool(
        effect=Effect.EXTERNAL_WRITE,
        min_tier=Tier.CLOUD,
        offline_policy=OfflinePolicy.DEFER,
        idempotency_key=lambda a: f"restock:{a['sku']}:{a['window']}",
        ttl_seconds=3600,
    )
    def place_restock_order(sku: str, qty: int, window: str) -> None:
        return None

    rules = RulesBackend(
        [Rule("restock", "place_restock_order", lambda t: {"sku": "SKU-991", "qty": 1, "window": "w1"})]
    )
    router = ModelRouter(registry, cloud=rules, local=rules, rules=rules)
    tier_resolver = TierResolver()
    loop = AgentLoop(tier_resolver, router, PolicyEngine(registry), journal=journal)

    with disk_exhausted(journal):
        with pytest.raises(JournalUnavailable):
            loop.step("restock SKU-991")

    assert tier_resolver.tier is Tier.JOURNAL_DOWN

    # recovery is explicit -- nothing auto-restores tier once the fault clears
    journal.append(_intent())
    tier_resolver.set_journal_available(True)
    assert tier_resolver.tier is Tier.LOCAL


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
