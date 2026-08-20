"""Reconciler: on reconnect, revalidate the pending intent queue and emit a
reviewable approval batch (docs/blackout-design.md §2.6).

    1. Load pending intents ordered by ULID.
    2. Mark anything past its TTL as expired.
    3. Topologically sort by depends_on; drop descendants of dead parents.
    4. Collapse duplicates by idempotency_key (keep earliest).
    5. Re-evaluate preconditions and classify: ready / ready_with_drift / rejected.
    6. Detect intra-batch conflicts (two intents writing the same resource).
    7. Emit the approval batch.

Ready and ready_with_drift are not journal statuses -- those intents stay
PENDING (they still need human approval via the approval-surface CLI).
Rejected/expired/collapsed/conflicted are terminal outcomes written back to
the journal via journal.resolve() as reconciliation proceeds.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .journal import Intent, IntentJournal, IntentStatus
from .policy import ToolPolicy, ToolRegistry
from .read_cache import PreconditionRegistry


@dataclass(frozen=True, slots=True)
class PreconditionDrift:
    name: str
    snapshot_value: Any
    current_value: Any


@dataclass(frozen=True, slots=True)
class ReadyWithDrift:
    intent: Intent
    drift: list[PreconditionDrift]


@dataclass(frozen=True, slots=True)
class ApprovalBatch:
    ready: list[Intent] = field(default_factory=list)
    ready_with_drift: list[ReadyWithDrift] = field(default_factory=list)
    rejected: list[Intent] = field(default_factory=list)
    collapsed: list[Intent] = field(default_factory=list)
    conflicts: list[list[Intent]] = field(default_factory=list)


def _resolve(journal: IntentJournal, intent: Intent, status: IntentStatus, note: str) -> None:
    """Write a terminal outcome back to the journal and reflect it on the
    in-memory intent, so the caller's batch and the journal never disagree."""
    journal.resolve(intent.id, status, note)
    intent.status = status
    intent.resolution_note = note


def _classify(
    intent: Intent, tool_policy: ToolPolicy, preconditions: PreconditionRegistry
) -> tuple[str, list[PreconditionDrift], str]:
    """Re-evaluate one intent's preconditions against fresh reads.

    Returns ("ready" | "ready_with_drift" | "rejected", drift, note).
    """
    if not tool_policy.preconditions:
        return "ready", [], ""

    fresh = preconditions.evaluate_many(tool_policy.preconditions, intent.args)

    unsatisfied = [p.name for p in fresh if not p.satisfied]
    if unsatisfied:
        return "rejected", [], f"precondition_unsatisfied: {', '.join(unsatisfied)}"

    limit = tool_policy.max_precondition_staleness_s
    worst = max((p.source_age_s for p in fresh), default=0.0)
    if limit is not None and worst > limit:
        return "rejected", [], f"precondition_too_stale_at_reconcile: {worst:.0f}s old, limit {limit:.0f}s"

    drift = [
        PreconditionDrift(
            name=p.name,
            snapshot_value=intent.precondition_snapshot[p.name]["value"],
            current_value=p.value,
        )
        for p in fresh
        if p.value != intent.precondition_snapshot[p.name]["value"]
    ]
    return ("ready_with_drift" if drift else "ready"), drift, ""


def _collapse_duplicates(
    journal: IntentJournal, pending: list[Intent]
) -> tuple[list[Intent], list[Intent]]:
    """Keep the earliest intent per idempotency_key, collapse the rest.

    `pending` is already in ULID (creation) order, so the first occurrence
    of a key is the earliest by construction.
    """
    seen: dict[str, Intent] = {}
    survivors: list[Intent] = []
    collapsed: list[Intent] = []
    for intent in pending:
        kept = seen.get(intent.idempotency_key)
        if kept is None:
            seen[intent.idempotency_key] = intent
            survivors.append(intent)
        else:
            _resolve(journal, intent, IntentStatus.COLLAPSED, f"duplicate of {kept.id}")
            collapsed.append(intent)
    return survivors, collapsed


_DEAD_STATUSES = (
    IntentStatus.REJECTED,
    IntentStatus.EXPIRED,
    IntentStatus.CONFLICTED,
    IntentStatus.ORPHANED,
)


def _existing_dead_ids(journal: IntentJournal) -> set[str]:
    """IDs of intents that are already terminal-and-not-viable -- from a
    previous reconcile run, an approval-inbox rejection, or expiry applied
    as a side effect of this run's own journal.pending() call above."""
    ids = {i.id for status in _DEAD_STATUSES for i in journal.by_status(status)}
    ids.update(r["id"] for r in journal.corrupt_records())
    return ids


def _topo_order(intents: list[Intent]) -> list[Intent]:
    """Parents before children, by depends_on edges within this batch.

    This ordering is what makes a same-run cascade correct: walking parents
    before children means a parent rejected earlier in this same walk is
    already in `dead_ids` by the time its child is classified. A cycle
    (should not happen) falls back to appending the leftover nodes in their
    original order rather than raising -- a chaos-tolerant system should
    degrade, not crash, on a data anomaly it didn't expect.
    """
    by_id = {i.id: i for i in intents}
    indegree = {i.id: 0 for i in intents}
    children: dict[str, list[str]] = {i.id: [] for i in intents}
    for intent in intents:
        for dep in intent.depends_on:
            if dep in by_id:
                children[dep].append(intent.id)
                indegree[intent.id] += 1

    queue = deque(i.id for i in intents if indegree[i.id] == 0)
    order: list[str] = []
    while queue:
        node_id = queue.popleft()
        order.append(node_id)
        for child_id in children[node_id]:
            indegree[child_id] -= 1
            if indegree[child_id] == 0:
                queue.append(child_id)

    if len(order) != len(intents):
        order.extend(i.id for i in intents if i.id not in order)

    return [by_id[i] for i in order]


def _detect_conflicts(
    journal: IntentJournal, registry: ToolRegistry, intents: list[Intent]
) -> list[list[Intent]]:
    """Group surviving intents by resource_key; any group with more than one
    member is an intra-batch conflict (two writes to the same resource)."""
    groups: dict[str, list[Intent]] = {}
    for intent in intents:
        resource_key = registry.policy(intent.tool).resource_key
        if resource_key is None:
            continue
        groups.setdefault(resource_key(intent.args), []).append(intent)

    conflicts = [group for group in groups.values() if len(group) > 1]
    for group in conflicts:
        for intent in group:
            _resolve(
                journal,
                intent,
                IntentStatus.CONFLICTED,
                "conflicts with another pending intent over the same resource",
            )
    return conflicts


def reconcile(
    journal: IntentJournal,
    registry: ToolRegistry,
    preconditions: PreconditionRegistry,
    now_ns: int | None = None,
) -> ApprovalBatch:
    journal.verify_all()
    pending = journal.pending(now_ns)
    survivors, collapsed = _collapse_duplicates(journal, pending)
    order = _topo_order(survivors)
    dead_ids = _existing_dead_ids(journal)

    ready: list[Intent] = []
    ready_with_drift: list[ReadyWithDrift] = []
    rejected: list[Intent] = []
    for intent in order:
        dead_dep = next((d for d in intent.depends_on if d in dead_ids), None)
        if dead_dep is not None:
            _resolve(
                journal,
                intent,
                IntentStatus.REJECTED,
                f"depends on {dead_dep}, which did not survive reconciliation",
            )
            rejected.append(intent)
            dead_ids.add(intent.id)
            continue

        tool_policy = registry.policy(intent.tool)
        outcome, drift, note = _classify(intent, tool_policy, preconditions)
        if outcome == "rejected":
            _resolve(journal, intent, IntentStatus.REJECTED, note)
            rejected.append(intent)
            dead_ids.add(intent.id)
        elif outcome == "ready_with_drift":
            ready_with_drift.append(ReadyWithDrift(intent=intent, drift=drift))
        else:
            ready.append(intent)

    conflicts = _detect_conflicts(
        journal, registry, ready + [rwd.intent for rwd in ready_with_drift]
    )
    conflicted_ids = {intent.id for group in conflicts for intent in group}
    ready = [i for i in ready if i.id not in conflicted_ids]
    ready_with_drift = [rwd for rwd in ready_with_drift if rwd.intent.id not in conflicted_ids]

    # §2.7: shakiest justifications reviewed first.
    ready.sort(key=lambda i: i.max_source_age_s, reverse=True)
    ready_with_drift.sort(key=lambda rwd: rwd.intent.max_source_age_s, reverse=True)

    return ApprovalBatch(
        ready=ready,
        ready_with_drift=ready_with_drift,
        rejected=rejected,
        collapsed=collapsed,
        conflicts=conflicts,
    )
