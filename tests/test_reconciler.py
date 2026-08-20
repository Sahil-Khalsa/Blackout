"""Reconciler (docs/blackout-design.md §2.6): on reconnect, revalidate the
pending intent queue -- expire, order by dependency, collapse duplicates,
re-evaluate preconditions against fresh reads, detect resource conflicts --
and emit a reviewable approval batch.

Ready and ready-with-drift intents stay PENDING in the journal (they still
need human approval via the not-yet-built CLI); rejected/expired/collapsed/
conflicted are journal-terminal outcomes written via journal.resolve().
"""

from blackout_core import (
    Intent,
    IntentJournal,
    IntentStatus,
    PreconditionRegistry,
    PreconditionValue,
    ReadCache,
)
from blackout_core.reconciler import ApprovalBatch, PreconditionDrift, ReadyWithDrift, reconcile


def _cache_and_preconditions(clock=None) -> tuple[ReadCache, PreconditionRegistry]:
    cache = ReadCache(clock) if clock is not None else ReadCache()
    preconditions = PreconditionRegistry(cache)
    preconditions.register(
        "inventory.below_threshold",
        cache_key=lambda a: f"inventory:{a['sku']}",
        predicate=lambda v: v["level"] < 10,
    )
    return cache, preconditions


class FakeClock:
    """Monotonic-shaped fake clock so staleness tests don't race real time."""

    def __init__(self, start_ns: int = 0) -> None:
        self.t = start_ns

    def __call__(self) -> int:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += int(seconds * 1e9)


def test_reconcile_empty_journal_returns_empty_batch(registry, tmp_path):
    journal = IntentJournal(tmp_path / "journal.db")
    _, preconditions = _cache_and_preconditions()

    batch = reconcile(journal, registry, preconditions)

    assert batch == ApprovalBatch()


def test_intent_with_no_declared_preconditions_is_ready_and_stays_pending(registry, tmp_path):
    journal = IntentJournal(tmp_path / "journal.db")
    _, preconditions = _cache_and_preconditions()

    intent = journal.append(
        Intent.from_evaluation(
            tool="page_oncall",
            args={"oncall": "sre-jane"},
            idempotency_key="page:sre-jane",
            tier_at_creation=2,
            ttl_seconds=3600,
        )
    )

    batch = reconcile(journal, registry, preconditions)

    assert [i.id for i in batch.ready] == [intent.id]
    assert journal.pending()[0].id == intent.id


def test_expired_intent_is_excluded_and_marked_expired_in_journal(registry, tmp_path):
    journal = IntentJournal(tmp_path / "journal.db")
    _, preconditions = _cache_and_preconditions()

    intent = journal.append(
        Intent.from_evaluation(
            tool="page_oncall",
            args={"oncall": "sre-jane"},
            idempotency_key="page:sre-jane",
            tier_at_creation=2,
            ttl_seconds=10,
            created_mono_ns=0,
        )
    )

    batch = reconcile(journal, registry, preconditions, now_ns=100 * 10**9)

    assert batch.ready == []
    assert journal.by_status(IntentStatus.EXPIRED)[0].id == intent.id


def test_precondition_identical_to_snapshot_is_ready(registry, tmp_path):
    journal = IntentJournal(tmp_path / "journal.db")
    cache, preconditions = _cache_and_preconditions()
    cache.put("inventory:991", {"sku": "991", "level": 4})

    intent = journal.append(
        Intent.from_evaluation(
            tool="place_restock_order",
            args={"sku": "991", "qty": 40, "window": "next-week"},
            idempotency_key="restock:991:next-week",
            tier_at_creation=2,
            ttl_seconds=3600,
            preconditions=[
                PreconditionValue(
                    name="inventory.below_threshold",
                    value={"sku": "991", "level": 4},
                    source_age_s=5.0,
                    satisfied=True,
                )
            ],
        )
    )

    batch = reconcile(journal, registry, preconditions)

    assert [i.id for i in batch.ready] == [intent.id]
    assert batch.ready_with_drift == []
    assert batch.rejected == []


def test_precondition_changed_but_still_satisfied_is_ready_with_drift(registry, tmp_path):
    journal = IntentJournal(tmp_path / "journal.db")
    cache, preconditions = _cache_and_preconditions()
    cache.put("inventory:991", {"sku": "991", "level": 6})  # was 4, still below threshold 10

    intent = journal.append(
        Intent.from_evaluation(
            tool="place_restock_order",
            args={"sku": "991", "qty": 40, "window": "next-week"},
            idempotency_key="restock:991:next-week",
            tier_at_creation=2,
            ttl_seconds=3600,
            preconditions=[
                PreconditionValue(
                    name="inventory.below_threshold",
                    value={"sku": "991", "level": 4},
                    source_age_s=5.0,
                    satisfied=True,
                )
            ],
        )
    )

    batch = reconcile(journal, registry, preconditions)

    assert batch.ready == []
    assert batch.rejected == []
    assert batch.ready_with_drift == [
        ReadyWithDrift(
            intent=intent,
            drift=[
                PreconditionDrift(
                    name="inventory.below_threshold",
                    snapshot_value={"sku": "991", "level": 4},
                    current_value={"sku": "991", "level": 6},
                )
            ],
        )
    ]
    assert journal.pending()[0].id == intent.id


def test_precondition_no_longer_satisfied_is_rejected(registry, tmp_path):
    journal = IntentJournal(tmp_path / "journal.db")
    cache, preconditions = _cache_and_preconditions()
    cache.put("inventory:991", {"sku": "991", "level": 15})  # already restocked

    intent = journal.append(
        Intent.from_evaluation(
            tool="place_restock_order",
            args={"sku": "991", "qty": 40, "window": "next-week"},
            idempotency_key="restock:991:next-week",
            tier_at_creation=2,
            ttl_seconds=3600,
            preconditions=[
                PreconditionValue(
                    name="inventory.below_threshold",
                    value={"sku": "991", "level": 4},
                    source_age_s=5.0,
                    satisfied=True,
                )
            ],
        )
    )

    batch = reconcile(journal, registry, preconditions)

    assert batch.ready == []
    assert batch.ready_with_drift == []
    assert [i.id for i in batch.rejected] == [intent.id]
    assert journal.by_status(IntentStatus.REJECTED)[0].id == intent.id


def test_fresh_precondition_too_stale_at_reconcile_time_is_rejected(registry, tmp_path):
    """A precondition that's satisfied right now but whose evidence is older
    than the tool's max_precondition_staleness_s isn't a real justification
    either -- §2.7's staleness rule applies at reconcile time too, not just
    at capture time."""
    journal = IntentJournal(tmp_path / "journal.db")
    clock = FakeClock()
    cache, preconditions = _cache_and_preconditions(clock)
    cache.put("inventory:991", {"sku": "991", "level": 4})

    intent = journal.append(
        Intent.from_evaluation(
            tool="place_restock_order",
            args={"sku": "991", "qty": 40, "window": "next-week"},
            idempotency_key="restock:991:next-week",
            tier_at_creation=2,
            ttl_seconds=3600,
            preconditions=[
                PreconditionValue(
                    name="inventory.below_threshold",
                    value={"sku": "991", "level": 4},
                    source_age_s=5.0,
                    satisfied=True,
                )
            ],
        )
    )

    clock.advance(4000)  # place_restock_order's max_precondition_staleness_s is 3600

    batch = reconcile(journal, registry, preconditions)

    assert batch.ready == []
    assert batch.ready_with_drift == []
    assert [i.id for i in batch.rejected] == [intent.id]


def test_duplicate_intents_collapse_keeping_earliest(registry, tmp_path):
    journal = IntentJournal(tmp_path / "journal.db")
    cache, preconditions = _cache_and_preconditions()
    cache.put("inventory:991", {"sku": "991", "level": 4})

    def _restock_intent() -> Intent:
        return Intent.from_evaluation(
            tool="place_restock_order",
            args={"sku": "991", "qty": 40, "window": "next-week"},
            idempotency_key="restock:991:next-week",
            tier_at_creation=2,
            ttl_seconds=3600,
            preconditions=[
                PreconditionValue(
                    name="inventory.below_threshold",
                    value={"sku": "991", "level": 4},
                    source_age_s=5.0,
                    satisfied=True,
                )
            ],
        )

    earliest = journal.append(_restock_intent())
    duplicate = journal.append(_restock_intent())

    batch = reconcile(journal, registry, preconditions)

    assert [i.id for i in batch.ready] == [earliest.id]
    assert [i.id for i in batch.collapsed] == [duplicate.id]
    assert journal.by_status(IntentStatus.COLLAPSED)[0].id == duplicate.id
    assert journal.pending()[0].id == earliest.id


def test_intent_depending_on_already_rejected_intent_cascades_rejected(registry, tmp_path):
    journal = IntentJournal(tmp_path / "journal.db")
    _, preconditions = _cache_and_preconditions()

    dead_parent = journal.append(
        Intent.from_evaluation(
            tool="page_oncall",
            args={"oncall": "sre-jane"},
            idempotency_key="page:sre-jane",
            tier_at_creation=2,
            ttl_seconds=3600,
        )
    )
    journal.resolve(dead_parent.id, IntentStatus.REJECTED, "rejected in a previous reconcile run")

    dependent = journal.append(
        Intent.from_evaluation(
            tool="page_oncall",
            args={"oncall": "sre-bob"},
            idempotency_key="page:sre-bob",
            tier_at_creation=2,
            ttl_seconds=3600,
            depends_on=[dead_parent.id],
        )
    )

    batch = reconcile(journal, registry, preconditions)

    assert batch.ready == []
    assert [i.id for i in batch.rejected] == [dependent.id]
    assert {i.id for i in journal.by_status(IntentStatus.REJECTED)} == {dead_parent.id, dependent.id}


def test_intent_depending_on_ready_intent_is_classified_normally(registry, tmp_path):
    journal = IntentJournal(tmp_path / "journal.db")
    _, preconditions = _cache_and_preconditions()

    parent = journal.append(
        Intent.from_evaluation(
            tool="page_oncall",
            args={"oncall": "sre-jane"},
            idempotency_key="page:sre-jane",
            tier_at_creation=2,
            ttl_seconds=3600,
        )
    )
    child = journal.append(
        Intent.from_evaluation(
            tool="page_oncall",
            args={"oncall": "sre-bob"},
            idempotency_key="page:sre-bob",
            tier_at_creation=2,
            ttl_seconds=3600,
            depends_on=[parent.id],
        )
    )

    batch = reconcile(journal, registry, preconditions)

    assert {i.id for i in batch.ready} == {parent.id, child.id}
    assert batch.rejected == []


def test_conflicting_intents_writing_same_resource_are_marked_conflicted(registry, tmp_path):
    journal = IntentJournal(tmp_path / "journal.db")
    cache, preconditions = _cache_and_preconditions()
    cache.put("inventory:991", {"sku": "991", "level": 4})

    def _restock_intent(window: str) -> Intent:
        return Intent.from_evaluation(
            tool="place_restock_order",
            args={"sku": "991", "qty": 40, "window": window},
            idempotency_key=f"restock:991:{window}",
            tier_at_creation=2,
            ttl_seconds=3600,
            preconditions=[
                PreconditionValue(
                    name="inventory.below_threshold",
                    value={"sku": "991", "level": 4},
                    source_age_s=5.0,
                    satisfied=True,
                )
            ],
        )

    first = journal.append(_restock_intent("next-week"))
    second = journal.append(_restock_intent("following-week"))

    batch = reconcile(journal, registry, preconditions)

    assert batch.ready == []
    assert batch.ready_with_drift == []
    assert batch.rejected == []
    assert len(batch.conflicts) == 1
    assert {i.id for i in batch.conflicts[0]} == {first.id, second.id}
    assert {i.id for i in journal.by_status(IntentStatus.CONFLICTED)} == {first.id, second.id}


def test_ready_batch_sorted_by_staleness_descending(registry, tmp_path):
    """§2.7: the approval batch is sorted by max_source_age_s descending, so
    the shakiest justifications get reviewed first."""
    journal = IntentJournal(tmp_path / "journal.db")
    cache, preconditions = _cache_and_preconditions()
    cache.put("inventory:100", {"sku": "100", "level": 4})
    cache.put("inventory:200", {"sku": "200", "level": 4})

    def _restock_intent(sku: str, source_age_s: float) -> Intent:
        return Intent.from_evaluation(
            tool="place_restock_order",
            args={"sku": sku, "qty": 40, "window": "next-week"},
            idempotency_key=f"restock:{sku}:next-week",
            tier_at_creation=2,
            ttl_seconds=3600,
            preconditions=[
                PreconditionValue(
                    name="inventory.below_threshold",
                    value={"sku": sku, "level": 4},
                    source_age_s=source_age_s,
                    satisfied=True,
                )
            ],
        )

    # Appended fresh-first, so creation order alone would put fresh first --
    # the assertion below only holds if reconcile() actually sorts by
    # staleness rather than preserving ULID order.
    fresh = journal.append(_restock_intent("200", 30.0))
    stale = journal.append(_restock_intent("100", 1800.0))

    batch = reconcile(journal, registry, preconditions)

    assert [i.id for i in batch.ready] == [stale.id, fresh.id]
