"""Wires mock backend, agent, Toxiproxy, injection, and scenario together
(docs/blackout-design.md §3, spec §7): run one scenario, produce a
ScenarioResult with every detector named in scenario.assert_ evaluated.

The assert_ strings are §3.2's own outcome-shaped names, not the detector
function names -- run_scenario owns the explicit mapping (spec §7's table)
since the two vocabularies don't match by construction.

After reconciliation, run_scenario approves every intent the reconciler
classified ready or ready_with_drift (cli.approve executes the deferred
call) -- without this, reconciliation alone never touches the mock
backend, and duplicate-effect/fabrication would have nothing in the
ledger to check against for any DEFER-based scenario.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from blackout_core import IntentJournal, PreconditionRegistry, ToolRegistry, reconcile
from blackout_core.cli import approve

from .agent import ChaosAgent
from . import injection
from .detectors import (
    DetectorResult,
    RunObservation,
    detect_authority_violation,
    detect_duplicate_effect,
    detect_fabrication,
    detect_lost_work,
    detect_silent_degradation,
)
from .mock_backend import MockBackendServer
from .scenario import Scenario

MODEL_PROXY_NAME = "chaos_model_proxy"
TOOL_PROXY_NAME = "chaos_tool_proxy"

_INJECTORS = {
    "pre_plan": injection.pre_plan,
    "mid_plan": injection.mid_plan,
    "post_request_pre_response": injection.post_request_pre_response,
    "partial_response": injection.partial_response,
    "slow_success": injection.slow_success,
    "flapping": injection.flapping,
    "recovery_storm": injection.recovery_storm,
}
_MODEL_PROXY_FAULTS = {"pre_plan", "mid_plan"}

_ASSERT_TO_DETECTOR = {
    "no_fabricated_results": "fabrication",
    "no_duplicate_effects": "duplicate_effect",
    "state_disclosed_to_user": "silent_degradation",
    "journal_consistent": "lost_work",
    "no_authority_violation": "authority_violation",
}


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    scenario: str
    detectors: dict[str, DetectorResult]


def _proxy_for(inject_at: str) -> str:
    return MODEL_PROXY_NAME if inject_at in _MODEL_PROXY_FAULTS else TOOL_PROXY_NAME


def _run_detector(name: str, obs: RunObservation, registry: ToolRegistry) -> DetectorResult:
    if name == "fabrication":
        return detect_fabrication(obs)
    if name == "duplicate_effect":
        return detect_duplicate_effect(obs)
    if name == "silent_degradation":
        return detect_silent_degradation(obs)
    if name == "lost_work":
        return detect_lost_work(obs)
    if name == "authority_violation":
        return detect_authority_violation(obs, registry)
    raise ValueError(f"unknown detector {name!r}")


def run_scenario(
    scenario: Scenario,
    agent: ChaosAgent,
    mock_backend: MockBackendServer,
    toxiproxy: Any,
    registry: ToolRegistry,
    journal: IntentJournal,
    preconditions: PreconditionRegistry,
) -> ScenarioResult:
    mock_backend.reset()
    for sku, level in scenario.seed_inventory.items():
        mock_backend.seed_inventory(sku, level)

    if scenario.warmup_task:
        agent.run_task(scenario.warmup_task)

    injector_fn = _INJECTORS[scenario.inject.at]
    proxy = _proxy_for(scenario.inject.at)
    with injector_fn(toxiproxy, proxy):
        agent.run_task(scenario.task)
        pending_ids_at_partition = agent.pending_work_ids()

    batch = reconcile(journal, registry, preconditions)
    for intent in list(batch.ready) + [rwd.intent for rwd in batch.ready_with_drift]:
        try:
            approve(journal, registry, intent)
        except Exception:
            pass  # left PENDING by cli.approve's own contract -- not lost, still counted below

    obs = RunObservation(
        tool_calls=agent.tool_calls(),
        disclosures=agent.disclosures(),
        tier_transitions=agent.tier_transitions() if hasattr(agent, "tier_transitions") else [],
        pending_ids_at_partition=pending_ids_at_partition,
        pending_ids_after=agent.pending_work_ids(),
        resolved_ids_after=agent.resolved_work_ids(),
        ledger=mock_backend.ledger(),
    )

    detectors = {
        assert_name: _run_detector(_ASSERT_TO_DETECTOR[assert_name], obs, registry)
        for assert_name in scenario.assert_
    }
    return ScenarioResult(scenario=scenario.scenario, detectors=detectors)
