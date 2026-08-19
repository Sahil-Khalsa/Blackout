"""End-to-end agent loop test reproducing the Week 1 milestone from
docs/blackout-design.md §5: pull the network mid-task and the agent refuses
a tier-1-only tool instead of hanging or hallucinating -- with no fabricated
success, and no reliance on a model backend being honest about what it
should call.

Uses RulesBackend as a stand-in "model" so this stays fast and hermetic; the
live-Ollama version of the same claim is in test_ollama_integration.py.
"""

from blackout_core import (
    AgentLoop,
    Decision,
    IntentJournal,
    ModelRouter,
    PolicyEngine,
    PreconditionRegistry,
    ReadCache,
    Rule,
    RulesBackend,
    Tier,
    TierResolver,
    ToolCall,
)


def _page_rule() -> Rule:
    return Rule(contains="page", tool="page_oncall", args=lambda t: {"oncall": "sre-jane"})


def _stock_rules() -> list[Rule]:
    return [
        Rule(contains="check stock", tool="read_inventory", args=lambda t: {"sku": "991"}),
        Rule(
            contains="restock",
            tool="place_restock_order",
            args=lambda t: {"sku": "991", "qty": 40, "window": "next-week"},
        ),
    ]


class FakeClock:
    """Monotonic-shaped fake clock so staleness tests don't race real time."""

    def __init__(self, start_ns: int = 0) -> None:
        self.t = start_ns

    def __call__(self) -> int:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += int(seconds * 1e9)


def test_agent_refuses_page_oncall_after_network_pull(registry):
    resolver = TierResolver(promote_after=3)
    for _ in range(3):
        resolver.record_probe(True, 0.1)
    assert resolver.tier is Tier.CLOUD

    backend = RulesBackend([_page_rule()])
    router = ModelRouter(registry, cloud=backend, local=backend, rules=backend)
    loop = AgentLoop(resolver, router, PolicyEngine(registry))

    step = loop.step("there is an outage, page the oncall")
    assert step.decision.decision is Decision.EXECUTE

    resolver.record_probe(False)  # pull the network
    assert resolver.tier is Tier.LOCAL

    step = loop.step("there is an outage, page the oncall")
    assert step.proposal is None  # structurally impossible, not refused after the fact
    assert step.decision is None
    assert step.error is None


def test_policy_refuses_if_a_misbehaving_backend_proposes_unauthorized_tool(registry):
    """Defense in depth: even if a backend implementation is buggy and
    proposes a tool it was never offered, PolicyEngine.evaluate is the final
    authority and refuses it -- the milestone doesn't rely on backend
    honesty alone."""

    class Rogue:
        def propose(self, tools, tier, task):
            return ToolCall(tool="page_oncall", args={"oncall": "sre-jane"}, raw="rogue")

    resolver = TierResolver(promote_after=1)
    assert resolver.tier is Tier.LOCAL  # default, no probes needed

    router = ModelRouter(registry, local=Rogue(), rules=RulesBackend())
    loop = AgentLoop(resolver, router, PolicyEngine(registry))

    step = loop.step("anything")
    assert step.decision.decision is Decision.REFUSE
    assert step.decision.reason == "refuse_below_tier"


def _stock_loop(registry, journal, cache) -> AgentLoop:
    preconditions = PreconditionRegistry(cache)
    preconditions.register(
        "inventory.below_threshold",
        cache_key=lambda a: f"inventory:{a['sku']}",
        predicate=lambda v: v["level"] < 10,
    )
    resolver = TierResolver()  # default tier LOCAL -- below place_restock_order's min_tier
    backend = RulesBackend(_stock_rules())
    router = ModelRouter(registry, cloud=backend, local=backend, rules=backend)
    return AgentLoop(
        resolver,
        router,
        PolicyEngine(registry),
        journal=journal,
        read_cache=cache,
        preconditions=preconditions,
    )


def test_defer_uses_precondition_from_populated_read_cache(registry, tmp_path):
    """Week 2 milestone: the loop fetches preconditions from its own read
    cache -- no caller-supplied PreconditionValue -- and produces a real
    DEFER with a genuine precondition snapshot in the journal."""
    journal = IntentJournal(tmp_path / "journal.db")
    cache = ReadCache()
    loop = _stock_loop(registry, journal, cache)

    read_step = loop.step("check stock for sku 991")
    assert read_step.decision.decision is Decision.EXECUTE
    assert cache.get("inventory:991") is not None

    restock_step = loop.step("please restock sku 991 for next week")
    assert restock_step.decision.decision is Decision.DEFER
    assert restock_step.intent_id is not None

    intent = journal.pending()[0]
    snapshot = intent.precondition_snapshot["inventory.below_threshold"]
    assert snapshot["value"] == {"sku": "991", "level": 4}
    assert snapshot["source_age_s"] < 1.0


def test_defer_refuses_when_cached_precondition_is_too_stale(registry, tmp_path):
    """A precondition that was already stale at capture time must REFUSE,
    not DEFER -- deferring on old evidence queues an intent nobody could
    responsibly approve later (docs/blackout-design.md §2.7)."""
    journal = IntentJournal(tmp_path / "journal.db")
    clock = FakeClock()
    cache = ReadCache(clock=clock)
    loop = _stock_loop(registry, journal, cache)

    read_step = loop.step("check stock for sku 991")
    assert read_step.decision.decision is Decision.EXECUTE

    clock.advance(4000)  # past place_restock_order's max_precondition_staleness_s=3600

    restock_step = loop.step("please restock sku 991 for next week")
    assert restock_step.decision.decision is Decision.REFUSE
    assert restock_step.decision.reason == "precondition_too_stale"
    assert restock_step.intent_id is None
    assert journal.pending() == []
