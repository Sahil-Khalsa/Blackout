"""End-to-end proof (docs/blackout-design.md §3, spec §1): the
write_ack_lost scenario running against a real Toxiproxy, a real
MockBackendServer, and a real blackout_core CoreAgentAdapter. Self-skips if
Toxiproxy isn't reachable, same convention as test_ollama_integration.py.

place_restock_order requires CLOUD tier to reach EXECUTE (its min_tier), so
this test primes the TierResolver to CLOUD directly via record_probe --
AgentLoop.step() never promotes tier on its own; tier promotion is driven by
external connectivity probes, not by a tool call succeeding.

Five green detector cells here are not proof of nothing: this is the first
test in the repo that drives an EXECUTE-tier tool call through a genuine
Toxiproxy-induced network failure. What it actually proves is narrower and
explicit -- see the assertions below -- because blackout_core's EXECUTE path
(loop.py::AgentLoop.step) has no exception handling around the tool call
itself: an ack-loss here produces no ToolCallRecord and no journaled intent,
so fabrication/duplicate-effect/lost-work structurally cannot fail against
this adapter. That's a real, documented blind spot, not a rigged pass -- it
starts mattering once a naive baseline (Week 4) is compared against it.
"""

from pathlib import Path

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
from blackout_chaos.runner import TOOL_PROXY_NAME, run_scenario
from blackout_chaos.scenario import load_scenario
from blackout_chaos.toxiproxy_client import ToxiproxyClient, toxiproxy_reachable

pytestmark = pytest.mark.skipif(not toxiproxy_reachable(), reason="Toxiproxy not reachable")

_SCENARIOS_DIR = Path(__file__).resolve().parent.parent / "blackout_chaos" / "scenarios"


def _rules_router(registry):
    rules = RulesBackend(
        [
            Rule("check inventory", "read_inventory", lambda t: {"sku": "SKU-991"}),
            Rule(
                "restock",
                "place_restock_order",
                lambda t: {"sku": "SKU-991", "qty": 50, "window": "2026-08-21"},
            ),
        ]
    )
    return ModelRouter(registry, cloud=rules, local=rules, rules=rules)


def test_write_ack_lost_end_to_end(tmp_path):
    server = MockBackendServer(host="0.0.0.0")
    server.start()
    toxiproxy = ToxiproxyClient()
    toxiproxy.reset()
    toxiproxy.create_proxy(
        TOOL_PROXY_NAME, listen="0.0.0.0:20001", upstream=f"host.docker.internal:{server.port}"
    )
    journal = IntentJournal(tmp_path / "journal.db")
    try:
        registry = build_mock_backend_registry("http://localhost:20001")
        cache = ReadCache()
        preconditions = PreconditionRegistry(cache)
        preconditions.register(
            "inventory.below_threshold",
            cache_key=lambda a: f"inventory:{a['sku']}",
            predicate=lambda v: v["level"] < 10,
        )
        tier_resolver = TierResolver(promote_after=1)
        tier_resolver.record_probe(True)  # prime to CLOUD -- place_restock_order's min_tier
        loop = AgentLoop(
            tier_resolver, _rules_router(registry), PolicyEngine(registry),
            journal=journal, read_cache=cache, preconditions=preconditions,
        )
        agent = CoreAgentAdapter(loop)

        scenario = load_scenario(_SCENARIOS_DIR / "write_ack_lost.yaml")
        result = run_scenario(scenario, agent, server, toxiproxy, registry, journal, preconditions)

        # the real finding: an EXECUTE-tier call crashed under fault
        # injection, and the harness knows it, even though no detector
        # below can express it.
        assert len(agent.errors()) == 1
        assert len(server.ledger()) == 1

        calls = agent.tool_calls()
        assert len(calls) == 1
        assert calls[0].tool == "read_inventory"
        assert calls[0].outcome == "executed"

        for name, detector_result in result.detectors.items():
            assert detector_result.passed, f"{name}: {detector_result.detail}"
    finally:
        toxiproxy.delete_proxy(TOOL_PROXY_NAME)
        journal.close()
        server.stop()
