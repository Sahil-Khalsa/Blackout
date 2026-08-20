"""Tests for runner.py -- orchestration logic, using a fake Toxiproxy admin
client so no Docker is needed. Everything else (MockBackendServer,
CoreAgentAdapter, IntentJournal, reconciler, cli.approve) is real."""

import pytest

from blackout_core import (
    AgentLoop,
    IntentJournal,
    ModelRouter,
    PolicyEngine,
    PreconditionRegistry,
    ReadCache,
    Rule,
    RulesBackend,
    TierResolver,
)

from blackout_chaos.agent import CoreAgentAdapter, build_mock_backend_registry
from blackout_chaos.mock_backend import MockBackendServer
from blackout_chaos.runner import MODEL_PROXY_NAME, TOOL_PROXY_NAME, run_scenario
from blackout_chaos.scenario import InjectSpec, Scenario


class _FakeToxiproxyClient:
    def __init__(self):
        self.calls = []

    def set_enabled(self, proxy, enabled):
        self.calls.append(("set_enabled", proxy, enabled))

    def add_toxic(self, proxy, name, type, stream="downstream", **attrs):
        self.calls.append(("add_toxic", proxy, name, type))

    def remove_toxic(self, proxy, name):
        self.calls.append(("remove_toxic", proxy, name))


def _rules_router(registry):
    rules = RulesBackend(
        [
            Rule("check inventory", "read_inventory", lambda t: {"sku": "SKU-991"}),
            Rule(
                "restock",
                "place_restock_order",
                lambda t: {"sku": "SKU-991", "qty": 50, "window": "w1"},
            ),
        ]
    )
    return ModelRouter(registry, cloud=rules, local=rules, rules=rules)


@pytest.fixture
def wiring(tmp_path):
    server = MockBackendServer()
    server.start()
    registry = build_mock_backend_registry(server.base_url)
    cache = ReadCache()
    preconditions = PreconditionRegistry(cache)
    preconditions.register(
        "inventory.below_threshold",
        cache_key=lambda a: f"inventory:{a['sku']}",
        predicate=lambda v: v["level"] < 10,
    )
    journal = IntentJournal(tmp_path / "journal.db")
    tier_resolver = TierResolver()  # stays at LOCAL: place_restock_order defers
    loop = AgentLoop(
        tier_resolver, _rules_router(registry), PolicyEngine(registry),
        journal=journal, read_cache=cache, preconditions=preconditions,
    )
    agent = CoreAgentAdapter(loop)
    yield agent, server, registry, journal, preconditions
    journal.close()
    server.stop()


def _scenario(**overrides):
    base = dict(
        scenario="defer_and_approve_test",
        description="test",
        task="restock SKU-991, we're almost out",
        warmup_task="check inventory for SKU-991",
        seed_inventory={"SKU-991": 2},
        inject=InjectSpec(at="post_request_pre_response", tool="place_restock_order", duration_s=1),
        assert_=["no_duplicate_effects", "no_fabricated_results", "journal_consistent"],
    )
    base.update(overrides)
    return Scenario(**base)


def test_run_scenario_applies_and_clears_the_named_toxic(wiring):
    agent, server, registry, journal, preconditions = wiring
    toxiproxy = _FakeToxiproxyClient()

    result = run_scenario(_scenario(), agent, server, toxiproxy, registry, journal, preconditions)

    assert ("add_toxic", TOOL_PROXY_NAME, "chaos_toxic", "timeout") in toxiproxy.calls
    assert ("remove_toxic", TOOL_PROXY_NAME, "chaos_toxic") in toxiproxy.calls
    assert result.scenario == "defer_and_approve_test"


def test_run_scenario_uses_model_proxy_for_pre_plan(wiring):
    agent, server, registry, journal, preconditions = wiring
    toxiproxy = _FakeToxiproxyClient()
    scenario = _scenario(inject=InjectSpec(at="pre_plan", tool="place_restock_order", duration_s=1))

    run_scenario(scenario, agent, server, toxiproxy, registry, journal, preconditions)

    assert any(call[1] == MODEL_PROXY_NAME for call in toxiproxy.calls)


def test_run_scenario_seeds_inventory_from_the_scenario(wiring):
    agent, server, registry, journal, preconditions = wiring
    toxiproxy = _FakeToxiproxyClient()

    run_scenario(_scenario(), agent, server, toxiproxy, registry, journal, preconditions)

    calls = agent.tool_calls()
    warmup_call = next(c for c in calls if c.tool == "read_inventory")
    assert warmup_call.result == {"sku": "SKU-991", "level": 2}


def test_run_scenario_approves_ready_intents_and_hits_the_ledger(wiring):
    agent, server, registry, journal, preconditions = wiring
    toxiproxy = _FakeToxiproxyClient()

    result = run_scenario(_scenario(), agent, server, toxiproxy, registry, journal, preconditions)

    assert len(server.ledger()) == 1
    assert result.detectors["no_duplicate_effects"].passed
    assert result.detectors["no_fabricated_results"].passed


def test_run_scenario_computes_pending_at_partition_before_reconcile(wiring):
    agent, server, registry, journal, preconditions = wiring
    toxiproxy = _FakeToxiproxyClient()

    result = run_scenario(_scenario(), agent, server, toxiproxy, registry, journal, preconditions)

    # the intent was deferred, then reconciled+approved -- accounted for,
    # not lost
    assert result.detectors["journal_consistent"].passed
