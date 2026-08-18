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
    ModelRouter,
    PolicyEngine,
    Rule,
    RulesBackend,
    Tier,
    TierResolver,
    ToolCall,
)


def _page_rule() -> Rule:
    return Rule(contains="page", tool="page_oncall", args=lambda t: {"oncall": "sre-jane"})


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
