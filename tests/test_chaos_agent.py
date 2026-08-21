"""Unit tests for CoreAgentAdapter -- pure except for one test that proves
build_mock_backend_registry's tools actually reach the network (no
Toxiproxy needed, just MockBackendServer). Uses the shared `registry`
fixture from tests/conftest.py for the pure tests."""

from blackout_core import (
    AgentLoop,
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


def test_tool_calls_empty_before_any_task(registry):
    loop = AgentLoop(TierResolver(), _rules_router(registry), PolicyEngine(registry))
    adapter = CoreAgentAdapter(loop)
    assert adapter.tool_calls() == []


def test_tool_calls_records_refused_call_with_no_result(registry):
    loop = AgentLoop(TierResolver(), _rules_router(registry), PolicyEngine(registry))
    adapter = CoreAgentAdapter(loop)
    adapter.run_task("restock SKU-991")
    calls = adapter.tool_calls()
    assert len(calls) == 1
    assert calls[0].tool == "place_restock_order"
    assert calls[0].outcome == "refused"
    assert calls[0].idempotency_key == "restock:SKU-991:w1"
    assert calls[0].result is None


def test_run_task_swallows_exception_and_records_it_in_errors():
    class ExplodingRouter:
        def propose(self, tier, task):
            raise RuntimeError("boom")

    loop = AgentLoop(TierResolver(), ExplodingRouter(), PolicyEngine(registry=None))
    adapter = CoreAgentAdapter(loop)

    adapter.run_task("anything")

    assert adapter.tool_calls() == []
    assert len(adapter.errors()) == 1
    assert "boom" in adapter.errors()[0]


def test_disclosures_and_tier_transitions_stay_in_lockstep():
    loop = AgentLoop(TierResolver(promote_after=1), RulesBackend(), PolicyEngine(registry=None))
    adapter = CoreAgentAdapter(loop)
    loop.tier_resolver.record_probe(True)
    assert len(adapter.disclosures()) == len(adapter.tier_transitions()) == 1


def test_mock_backend_registry_reaches_execute_and_hits_the_network():
    server = MockBackendServer()
    server.start()
    try:
        server.seed_inventory("SKU-991", 4)
        server_registry = build_mock_backend_registry(server.base_url)
        cache = ReadCache()
        preconditions = PreconditionRegistry(cache)
        preconditions.register(
            "inventory.below_threshold",
            cache_key=lambda a: f"inventory:{a['sku']}",
            predicate=lambda v: v["level"] < 10,
        )
        tier_resolver = TierResolver(promote_after=1)
        tier_resolver.record_probe(True)
        loop = AgentLoop(
            tier_resolver,
            _rules_router(server_registry),
            PolicyEngine(server_registry),
            read_cache=cache,
            preconditions=preconditions,
        )
        adapter = CoreAgentAdapter(loop)

        adapter.run_task("check inventory for SKU-991")
        adapter.run_task("restock SKU-991, we're almost out")

        calls = adapter.tool_calls()
        assert calls[-1].tool == "place_restock_order"
        assert calls[-1].outcome == "executed"
        assert len(server.ledger()) == 1
    finally:
        server.stop()
