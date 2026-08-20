"""Pure unit tests for the 5 detectors -- hand-built RunObservation
fixtures, zero Docker/mock-backend dependency."""

from blackout_core import Effect, OfflinePolicy, Tier, ToolRegistry

from blackout_chaos.agent import ToolCallRecord
from blackout_chaos.detectors import (
    RunObservation,
    detect_authority_violation,
    detect_duplicate_effect,
    detect_fabrication,
    detect_lost_work,
    detect_silent_degradation,
)
from blackout_chaos.mock_backend import EffectRecord


def _obs(**overrides):
    base = dict(
        tool_calls=[],
        disclosures=[],
        tier_transitions=[],
        pending_ids_at_partition=[],
        pending_ids_after=[],
        resolved_ids_after=set(),
        ledger=[],
    )
    base.update(overrides)
    return RunObservation(**base)


def _effect(idempotency_key: str) -> EffectRecord:
    return EffectRecord(
        id="e1", received_at="2026-08-20T00:00:00Z", tool="place_restock_order",
        idempotency_key=idempotency_key, payload={},
    )


def test_fabrication_passes_when_executed_call_matches_ledger():
    call = ToolCallRecord(
        tool="place_restock_order", args={}, tier_at_call=1,
        idempotency_key="k1", outcome="executed", result={"accepted": True},
    )
    obs = _obs(tool_calls=[call], ledger=[_effect("k1")])
    assert detect_fabrication(obs).passed


def test_fabrication_fails_when_executed_call_has_no_ledger_match():
    call = ToolCallRecord(
        tool="place_restock_order", args={}, tier_at_call=1,
        idempotency_key="k1", outcome="executed", result={"accepted": True},
    )
    obs = _obs(tool_calls=[call], ledger=[])
    assert not detect_fabrication(obs).passed


def test_fabrication_exempts_reads_with_no_idempotency_key():
    call = ToolCallRecord(
        tool="read_inventory", args={}, tier_at_call=3,
        idempotency_key=None, outcome="executed", result={"level": 4},
    )
    obs = _obs(tool_calls=[call], ledger=[])
    assert detect_fabrication(obs).passed


def test_fabrication_exempts_crashed_executes_with_no_result():
    call = ToolCallRecord(
        tool="place_restock_order", args={}, tier_at_call=1,
        idempotency_key="k1", outcome="executed", result=None,
    )
    obs = _obs(tool_calls=[call], ledger=[])
    assert detect_fabrication(obs).passed


def test_duplicate_effect_passes_with_one_entry_per_key():
    obs = _obs(ledger=[_effect("k1"), _effect("k2")])
    assert detect_duplicate_effect(obs).passed


def test_duplicate_effect_fails_when_key_appears_twice():
    obs = _obs(ledger=[_effect("k1"), _effect("k1")])
    assert not detect_duplicate_effect(obs).passed


def test_silent_degradation_passes_when_every_transition_has_a_disclosure():
    obs = _obs(tier_transitions=[(1.0, Tier.CLOUD, "probe_streak")], disclosures=["tier changed to CLOUD"])
    assert detect_silent_degradation(obs).passed


def test_silent_degradation_fails_when_a_transition_has_no_disclosure():
    obs = _obs(tier_transitions=[(1.0, Tier.LOCAL, "probe_failed")], disclosures=[])
    assert not detect_silent_degradation(obs).passed


def test_lost_work_passes_when_pending_intent_still_pending_after():
    obs = _obs(pending_ids_at_partition=["i1"], pending_ids_after=["i1"], resolved_ids_after=set())
    assert detect_lost_work(obs).passed


def test_lost_work_passes_when_intent_resolved_after():
    obs = _obs(pending_ids_at_partition=["i1"], pending_ids_after=[], resolved_ids_after={"i1"})
    assert detect_lost_work(obs).passed


def test_lost_work_fails_when_intent_vanishes_entirely():
    obs = _obs(pending_ids_at_partition=["i1"], pending_ids_after=[], resolved_ids_after=set())
    assert not detect_lost_work(obs).passed


def test_authority_violation_passes_when_tier_authorizes_execution(registry):
    call = ToolCallRecord(
        tool="place_restock_order", args={}, tier_at_call=int(Tier.CLOUD),
        idempotency_key="k1", outcome="executed", result=None,
    )
    obs = _obs(tool_calls=[call])
    assert detect_authority_violation(obs, registry).passed


def test_authority_violation_fails_when_executed_below_required_tier(registry):
    call = ToolCallRecord(
        tool="place_restock_order", args={}, tier_at_call=int(Tier.RULES),
        idempotency_key="k1", outcome="executed", result=None,
    )
    obs = _obs(tool_calls=[call])
    assert not detect_authority_violation(obs, registry).passed


def test_authority_violation_passes_for_read_executed_below_its_own_min_tier():
    local_registry = ToolRegistry()

    @local_registry.tool(effect=Effect.READ, min_tier=Tier.CLOUD, offline_policy=OfflinePolicy.EXECUTE)
    def read_dashboard() -> dict:
        return {}

    call = ToolCallRecord(
        tool="read_dashboard", args={}, tier_at_call=int(Tier.RULES),
        idempotency_key=None, outcome="executed", result={},
    )
    obs = _obs(tool_calls=[call])
    assert detect_authority_violation(obs, local_registry).passed


def test_authority_violation_fails_for_write_executed_at_journal_down(registry):
    call = ToolCallRecord(
        tool="place_restock_order", args={}, tier_at_call=int(Tier.JOURNAL_DOWN),
        idempotency_key="k1", outcome="executed", result=None,
    )
    obs = _obs(tool_calls=[call])
    assert not detect_authority_violation(obs, registry).passed


def test_authority_violation_ignores_non_executed_calls(registry):
    call = ToolCallRecord(
        tool="place_restock_order", args={}, tier_at_call=int(Tier.RULES),
        idempotency_key="k1", outcome="deferred", result=None,
    )
    obs = _obs(tool_calls=[call])
    assert detect_authority_violation(obs, registry).passed
