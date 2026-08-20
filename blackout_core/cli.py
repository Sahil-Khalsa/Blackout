"""Approval-surface CLI: a minimal review surface over the reconciler's
ApprovalBatch (docs/blackout-design.md §2.6 line 40, §7 demo step 5).

Split into a testable core (build_inbox / format_inbox / approve / reject)
and a thin interactive shell (run_interactive) with input/output injected,
so the review loop can be driven without real stdin/stdout.

InboxView adds the two sections the reconciler itself doesn't produce, but
that the doc says belong in the approval inbox: orphaned intents (§2.8 --
missing/corrupt checkpoint, never auto-replayed) and corrupt records (§2.11
-- HMAC verification failed, genuinely unreviewable).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .journal import Intent, IntentJournal, IntentStatus
from .policy import ToolRegistry
from .read_cache import PreconditionRegistry
from .reconciler import ApprovalBatch, reconcile


@dataclass(slots=True)
class InboxView:
    batch: ApprovalBatch
    orphaned: list[Intent]
    corrupt: list[dict[str, Any]]


def build_inbox(
    journal: IntentJournal,
    registry: ToolRegistry,
    preconditions: PreconditionRegistry,
    now_ns: int | None = None,
) -> InboxView:
    batch = reconcile(journal, registry, preconditions, now_ns)
    orphaned = journal.by_status(IntentStatus.ORPHANED)
    corrupt = journal.corrupt_records()
    return InboxView(batch=batch, orphaned=orphaned, corrupt=corrupt)


def _format_intent(intent: Intent, registry: ToolRegistry) -> str:
    warning = "" if registry.policy(intent.tool).reversible else "  !! IRREVERSIBLE"
    return f"  [{intent.id}] {intent.tool}({intent.args}){warning}"


def format_inbox(view: InboxView, registry: ToolRegistry) -> str:
    lines = ["Ready:"]
    for intent in view.batch.ready:
        lines.append(_format_intent(intent, registry))

    lines.append("Ready (precondition drift):")
    for rwd in view.batch.ready_with_drift:
        lines.append(_format_intent(rwd.intent, registry))
        for d in rwd.drift:
            lines.append(f"      drift: {d.name}: {d.snapshot_value} -> {d.current_value}")

    lines.append("Rejected:")
    for intent in view.batch.rejected:
        lines.append(f"  [{intent.id}] {intent.tool} -- {intent.resolution_note}")

    lines.append("Collapsed (duplicate of an earlier intent):")
    for intent in view.batch.collapsed:
        lines.append(f"  [{intent.id}] {intent.tool} -- {intent.resolution_note}")

    lines.append("Conflicts (multiple intents writing the same resource):")
    for group in view.batch.conflicts:
        lines.append("  " + ", ".join(f"[{i.id}] {i.tool}" for i in group))

    lines.append("Orphaned (no checkpoint context -- review with caution):")
    for intent in view.orphaned:
        lines.append(_format_intent(intent, registry))

    lines.append("Corrupt (HMAC verification failed -- UNREVIEWABLE):")
    for record in view.corrupt:
        lines.append(f"  [{record['id']}] {record['tool']} -- {record['resolution_note']}")

    return "\n".join(lines)


def approve(journal: IntentJournal, registry: ToolRegistry, intent: Intent, note: str = "") -> Any:
    """Execute the deferred tool call, then mark the intent replayed.

    If the tool raises, the exception propagates and the intent is left
    PENDING rather than marked REPLAYED -- never claim an effect happened
    that didn't.
    """
    fn = registry.get(intent.tool).fn
    result = fn(**intent.args)
    journal.resolve(intent.id, IntentStatus.REPLAYED, note)
    intent.status = IntentStatus.REPLAYED
    return result


def reject(journal: IntentJournal, intent: Intent, note: str = "") -> None:
    journal.resolve(intent.id, IntentStatus.REJECTED, note)
    intent.status = IntentStatus.REJECTED


def run_interactive(
    journal: IntentJournal,
    registry: ToolRegistry,
    preconditions: PreconditionRegistry,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> None:
    """The actual per-item approve/reject/skip loop.

    Only intents still awaiting a human decision are prompted: ready,
    ready_with_drift, and orphaned (shown but flagged as lacking checkpoint
    context). Rejected/expired/collapsed/conflicted are already terminal,
    and corrupt records are never actionable -- none of those consume a
    response from input_fn.
    """
    view = build_inbox(journal, registry, preconditions)
    output_fn(format_inbox(view, registry))

    decidable = (
        list(view.batch.ready)
        + [rwd.intent for rwd in view.batch.ready_with_drift]
        + list(view.orphaned)
    )
    for intent in decidable:
        while True:
            choice = input_fn(f"[{intent.id}] {intent.tool} -- approve/reject/skip? (a/r/s) ").strip().lower()
            if choice in ("a", "approve"):
                approve(journal, registry, intent)
                output_fn(f"approved {intent.id}")
                break
            if choice in ("r", "reject"):
                reject(journal, intent)
                output_fn(f"rejected {intent.id}")
                break
            if choice in ("s", "skip"):
                output_fn(f"skipped {intent.id}")
                break
            output_fn("please enter a, r, or s")
