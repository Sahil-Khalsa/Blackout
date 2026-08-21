"""Pure disk-exhaustion tests for injection.py. Runs unconditionally, no
Docker and no `chaos` extra required. The 7 live network-toxic tests live in
test_chaos_injection_live.py, split out so an `import requests` needed only
by the live tests can't take collection of these down when `requests` isn't
installed (it is not a transitive dependency of the `dev`/`cloud` extras)."""

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
