"""Agent interface (docs/blackout-design.md §3, spec §4). The harness drives
and observes any agent through ChaosAgent; CoreAgentAdapter is the
blackout_core implementation.

run_task must never raise: a crash during EXECUTE (e.g. a lost ack under
fault injection) is caught and stored in errors(), not re-raised. This is
deliberately one step short of a fourth outcome value -- ToolCallRecord.
outcome stays exactly "executed" | "deferred" | "refused" per spec §4.1. A
crashed attempt produces no ToolCallRecord at all, the same convention
§4.1 already states for a step() that proposes nothing.

build_mock_backend_registry wires the example read_inventory /
place_restock_order tools to a running MockBackendServer over plain HTTP.
Uses stdlib urllib, not requests -- consistent with ollama_backend.py's
"the wire protocol is plain JSON over HTTP, so no dependency is needed" --
which is what lets this module stay outside the chaos extra.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from blackout_core import (
    Decision,
    Effect,
    IntentStatus,
    OfflinePolicy,
    Tier,
    ToolRegistry,
)


class ChaosAgent(Protocol):
    def run_task(self, task: str) -> None: ...
    def tool_calls(self) -> list[ToolCallRecord]: ...
    def disclosures(self) -> list[str]: ...
    def pending_work_ids(self) -> list[str]: ...
    def resolved_work_ids(self) -> set[str]: ...


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    tool: str
    args: dict[str, Any]
    tier_at_call: int
    idempotency_key: str | None
    outcome: str
    result: Any = None


_OUTCOME_NAMES = {
    Decision.EXECUTE: "executed",
    Decision.DEFER: "deferred",
    Decision.REFUSE: "refused",
}

_RESOLVED_STATUSES = (
    IntentStatus.REPLAYED,
    IntentStatus.REJECTED,
    IntentStatus.EXPIRED,
    IntentStatus.CONFLICTED,
    IntentStatus.COLLAPSED,
    IntentStatus.ORPHANED,
)


class CoreAgentAdapter:
    """Wraps an AgentLoop + its IntentJournal + TierResolver.

    resolved_work_ids() deliberately excludes CORRUPT -- a corrupted record
    is neither replayed nor explicitly rejected, so it should trip the
    lost-work detector by the detector's own literal definition, not be
    quietly counted as resolved.
    """

    def __init__(self, loop: Any) -> None:
        self.loop = loop
        self._results: list[Any] = []
        self._errors: list[str] = []

    def run_task(self, task: str) -> None:
        try:
            result = self.loop.step(task)
        except Exception as exc:  # ack-loss/crash is expected under fault injection
            self._errors.append(str(exc))
            return
        self._results.append(result)

    def errors(self) -> list[str]:
        return list(self._errors)

    def tool_calls(self) -> list[ToolCallRecord]:
        records = []
        for result in self._results:
            if result.proposal is None or result.decision is None:
                continue
            policy = self.loop.router.registry.policy(result.proposal.tool)
            idem = (
                policy.idempotency_key(result.proposal.args)
                if policy.idempotency_key
                else None
            )
            records.append(
                ToolCallRecord(
                    tool=result.proposal.tool,
                    args=dict(result.proposal.args),
                    tier_at_call=int(result.tier),
                    idempotency_key=idem,
                    outcome=_OUTCOME_NAMES[result.decision.decision],
                    result=result.result,
                )
            )
        return records

    def disclosures(self) -> list[str]:
        return [
            f"tier changed to {tier.name} ({reason})"
            for _, tier, reason in self.loop.tier_resolver.transitions
        ]

    def tier_transitions(self) -> list[tuple]:
        return list(self.loop.tier_resolver.transitions)

    def pending_work_ids(self) -> list[str]:
        if self.loop.journal is None:
            return []
        return [i.id for i in self.loop.journal.pending()]

    def resolved_work_ids(self) -> set[str]:
        if self.loop.journal is None:
            return set()
        ids: set[str] = set()
        for status in _RESOLVED_STATUSES:
            ids.update(i.id for i in self.loop.journal.by_status(status))
        return ids


def build_mock_backend_registry(base_url: str) -> ToolRegistry:
    """A ToolRegistry mirroring tests/conftest.py's example tools, backed by
    real HTTP calls to a running MockBackendServer instead of in-process
    stubs -- what lets the chaos harness exercise Toxiproxy faults against
    genuine network I/O. base_url may point directly at a MockBackendServer
    or at a Toxiproxy proxy in front of one.
    """
    registry = ToolRegistry()

    @registry.tool(
        effect=Effect.READ,
        min_tier=Tier.RULES,
        offline_policy=OfflinePolicy.EXECUTE,
        cache_key=lambda a: f"inventory:{a['sku']}",
    )
    def read_inventory(sku: str) -> dict:
        with urllib.request.urlopen(f"{base_url}/inventory/{sku}", timeout=5.0) as resp:
            return json.loads(resp.read())

    @registry.tool(
        effect=Effect.EXTERNAL_WRITE,
        min_tier=Tier.CLOUD,
        offline_policy=OfflinePolicy.DEFER,
        idempotency_key=lambda a: f"restock:{a['sku']}:{a['window']}",
        preconditions=["inventory.below_threshold"],
        max_precondition_staleness_s=3600,
        ttl_seconds=14400,
        reversible=False,
        resource_key=lambda a: f"sku:{a['sku']}",
    )
    def place_restock_order(sku: str, qty: int, window: str) -> dict:
        body = json.dumps(
            {
                "sku": sku,
                "qty": qty,
                "window": window,
                "idempotency_key": f"restock:{sku}:{window}",
            }
        ).encode()
        req = urllib.request.Request(
            f"{base_url}/restock",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return json.loads(resp.read())

    return registry
