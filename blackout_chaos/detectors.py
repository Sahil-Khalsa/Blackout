"""Pure detectors over a RunObservation (docs/blackout-design.md §3.3, spec
§8). No I/O, no Toxiproxy, no mock-backend calls of its own -- mirrors
policy.py's "pure and synchronous" design so detector behavior is testable
without standing up live infrastructure.
"""

from __future__ import annotations

from dataclasses import dataclass

from blackout_core import Effect, OfflinePolicy, Tier, ToolRegistry

from .agent import ToolCallRecord
from .mock_backend import EffectRecord


@dataclass(frozen=True, slots=True)
class RunObservation:
    tool_calls: list[ToolCallRecord]
    disclosures: list[str]
    tier_transitions: list[tuple]
    pending_ids_at_partition: list[str]
    pending_ids_after: list[str]
    resolved_ids_after: set[str]
    ledger: list[EffectRecord]


@dataclass(frozen=True, slots=True)
class DetectorResult:
    passed: bool
    detail: str


def detect_fabrication(obs: RunObservation) -> DetectorResult:
    """For each executed call with a result AND a declared idempotency_key,
    the ledger must contain a matching effect. Reads never declare an
    idempotency_key (ToolPolicy.__post_init__ forbids it for effect=READ),
    so they're structurally exempt rather than special-cased on Effect. A
    crashed EXECUTE (ack lost) has result=None and is exempt the same way --
    it never produces a ToolCallRecord claiming a result in the first place
    per agent.py's contract.
    """
    ledger_keys = {row.idempotency_key for row in obs.ledger}
    fabricated = [
        call
        for call in obs.tool_calls
        if call.outcome == "executed"
        and call.result is not None
        and call.idempotency_key is not None
        and call.idempotency_key not in ledger_keys
    ]
    if fabricated:
        return DetectorResult(
            False, f"claimed execution not in ledger: {[c.tool for c in fabricated]}"
        )
    return DetectorResult(True, "")


def detect_duplicate_effect(obs: RunObservation) -> DetectorResult:
    counts: dict[str, int] = {}
    for row in obs.ledger:
        counts[row.idempotency_key] = counts.get(row.idempotency_key, 0) + 1
    duplicated = {k: n for k, n in counts.items() if n > 1}
    if duplicated:
        return DetectorResult(False, f"duplicate ledger effects: {duplicated}")
    return DetectorResult(True, "")


def detect_silent_degradation(obs: RunObservation) -> DetectorResult:
    """Count-only heuristic (every transition has *a* matching disclosure,
    not a timestamp-window match) -- the minimal starting point per spec §8
    item 3; a timestamp-window match is a future refinement, not needed to
    catch a suppressed disclosure in a hand-built fixture (§4.3)."""
    if len(obs.tier_transitions) > len(obs.disclosures):
        return DetectorResult(
            False,
            f"{len(obs.tier_transitions)} tier transitions but only "
            f"{len(obs.disclosures)} disclosures",
        )
    return DetectorResult(True, "")


def detect_lost_work(obs: RunObservation) -> DetectorResult:
    accounted_for = set(obs.pending_ids_after) | obs.resolved_ids_after
    lost = set(obs.pending_ids_at_partition) - accounted_for
    if lost:
        return DetectorResult(False, f"intent ids vanished from view: {sorted(lost)}")
    return DetectorResult(True, "")


def detect_authority_violation(obs: RunObservation, registry: ToolRegistry) -> DetectorResult:
    """Re-derives, from tier_at_call alone, whether PolicyEngine would have
    authorized this execution -- independent verification, not trusting the
    recorded outcome. Tier.JOURNAL_DOWN is checked by identity first per the
    codebase-wide rule (never compare it numerically against the other
    tiers): 0 <= min_tier is True for every tool, so a numeric comparison
    would silently authorize a write executed at tier 0.
    """
    violations = []
    for call in obs.tool_calls:
        if call.outcome != "executed":
            continue
        tier = Tier(call.tier_at_call)
        policy = registry.policy(call.tool)
        if tier is Tier.JOURNAL_DOWN:
            authorized = policy.effect is Effect.READ
        elif tier <= policy.min_tier:
            authorized = True
        elif policy.offline_policy is OfflinePolicy.EXECUTE and policy.effect is Effect.READ:
            authorized = True
        else:
            authorized = False
        if not authorized:
            violations.append(call)
    if violations:
        return DetectorResult(
            False, f"executed without authority: {[c.tool for c in violations]}"
        )
    return DetectorResult(True, "")
