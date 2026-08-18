"""ModelRouter dispatch, including the tier-0 identity check."""

import pytest

from blackout_core import ModelRouter, Rule, RulesBackend, Tier, ToolCall


def test_journal_down_never_routes_to_cloud_or_local(registry):
    """JOURNAL_DOWN == 0 numerically, so a naive `tier <= Tier.LOCAL` check
    would misroute it to the local backend. Assert it goes to rules instead,
    and that cloud/local are never even consulted."""

    calls = []

    class Tripwire:
        def propose(self, tools, tier, task):
            calls.append(tier)
            return None

    router = ModelRouter(registry, cloud=Tripwire(), local=Tripwire(), rules=RulesBackend())
    router.propose(Tier.JOURNAL_DOWN, "anything")
    assert calls == []


def test_missing_backend_for_tier_raises(registry):
    router = ModelRouter(registry, cloud=None, local=None, rules=None)
    with pytest.raises(RuntimeError, match="no cloud backend"):
        router.propose(Tier.CLOUD, "task")


def test_rules_backend_only_matches_offered_tools(registry):
    backend = RulesBackend(
        [Rule(contains="page", tool="page_oncall", args=lambda t: {"oncall": "sre"})]
    )

    tier2_tools = registry.available_at(Tier.LOCAL)  # page_oncall excluded here
    assert backend.propose(tier2_tools, Tier.LOCAL, "please page the oncall") is None

    tier1_tools = registry.available_at(Tier.CLOUD)
    call = backend.propose(tier1_tools, Tier.CLOUD, "please page the oncall")
    assert call == ToolCall(tool="page_oncall", args={"oncall": "sre"}, raw="rule:page")
