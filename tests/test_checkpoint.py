"""CheckpointStore: durable task checkpoints alongside the intent journal
(docs/blackout-design.md §2.8). Same conventions as journal.py -- HMAC
tamper-evidence, its own SQLite connection to the journal's file path -- but
no monotonic/TTL logic, since checkpoints don't expire.
"""

import sqlite3

from blackout_core import Intent, IntentJournal, IntentStatus
from blackout_core.checkpoint import CheckpointStatus, CheckpointStore


def test_start_and_get_roundtrip(tmp_path):
    store = CheckpointStore(tmp_path / "journal.db")

    checkpoint = store.start("restock sweep for warehouse 4", reasoning_trace_id="trace-1")

    loaded = store.get(checkpoint.id)
    assert loaded is not None
    assert loaded.task == "restock sweep for warehouse 4"
    assert loaded.reasoning_trace_id == "trace-1"
    assert loaded.status is CheckpointStatus.ACTIVE
    assert loaded.completed_steps == []
    assert loaded.pending_plan == {}


def test_get_unknown_id_returns_none(tmp_path):
    store = CheckpointStore(tmp_path / "journal.db")
    assert store.get("does-not-exist") is None


def test_record_step_appends_to_completed_steps(tmp_path):
    store = CheckpointStore(tmp_path / "journal.db")
    checkpoint = store.start("restock sweep")

    store.record_step(checkpoint.id, {"tool": "read_inventory", "args": {"sku": "991"}})
    store.record_step(checkpoint.id, {"tool": "place_restock_order", "args": {"sku": "991"}})

    loaded = store.get(checkpoint.id)
    assert loaded.completed_steps == [
        {"tool": "read_inventory", "args": {"sku": "991"}},
        {"tool": "place_restock_order", "args": {"sku": "991"}},
    ]


def test_set_pending_plan_replaces_plan(tmp_path):
    store = CheckpointStore(tmp_path / "journal.db")
    checkpoint = store.start("restock sweep")

    store.set_pending_plan(checkpoint.id, {"remaining_skus": ["991", "1200"]})
    store.set_pending_plan(checkpoint.id, {"remaining_skus": ["1200"]})

    loaded = store.get(checkpoint.id)
    assert loaded.pending_plan == {"remaining_skus": ["1200"]}


def test_complete_sets_status_completed(tmp_path):
    store = CheckpointStore(tmp_path / "journal.db")
    checkpoint = store.start("restock sweep")

    store.complete(checkpoint.id)

    assert store.get(checkpoint.id).status is CheckpointStatus.COMPLETED


def test_abandon_sets_status_abandoned(tmp_path):
    store = CheckpointStore(tmp_path / "journal.db")
    checkpoint = store.start("restock sweep")

    store.abandon(checkpoint.id)

    assert store.get(checkpoint.id).status is CheckpointStatus.ABANDONED


def test_live_ids_includes_all_verified_checkpoints_regardless_of_status(tmp_path):
    store = CheckpointStore(tmp_path / "journal.db")
    active = store.start("task a")
    completed = store.start("task b")
    store.complete(completed.id)

    assert store.live_ids() == {active.id, completed.id}


def test_tampered_row_excluded_from_get_and_live_ids(tmp_path):
    path = tmp_path / "journal.db"
    store = CheckpointStore(path)
    checkpoint = store.start("restock sweep")

    raw = sqlite3.connect(path)
    raw.execute(
        "UPDATE checkpoints SET task = 'tampered task' WHERE id = ?", (checkpoint.id,)
    )
    raw.commit()
    raw.close()

    assert store.get(checkpoint.id) is None
    assert checkpoint.id not in store.live_ids()


def _intent(idempotency_key: str, task_checkpoint_id: str | None) -> Intent:
    return Intent.from_evaluation(
        tool="place_restock_order",
        args={"sku": "991"},
        idempotency_key=idempotency_key,
        tier_at_creation=2,
        ttl_seconds=3600,
        task_checkpoint_id=task_checkpoint_id,
    )


def test_orphan_missing_checkpoints_flags_intents_whose_checkpoint_is_missing_or_corrupt(tmp_path):
    """Simulates a restart: intents referencing a checkpoint that didn't
    survive (never written, or tampered) get flagged ORPHANED; one whose
    checkpoint is intact stays PENDING, and one with no checkpoint at all
    (never had a plan to lose) is untouched."""
    path = tmp_path / "journal.db"
    store = CheckpointStore(path)
    journal = IntentJournal(path)

    intact = store.start("intact task")
    corrupt = store.start("corrupt task")
    raw = sqlite3.connect(path)
    raw.execute("UPDATE checkpoints SET task = 'tampered' WHERE id = ?", (corrupt.id,))
    raw.commit()
    raw.close()

    journal.append(_intent("k-intact", intact.id))
    journal.append(_intent("k-corrupt", corrupt.id))
    journal.append(_intent("k-missing", "checkpoint-never-written"))
    journal.append(_intent("k-no-checkpoint", None))

    journal.orphan_missing_checkpoints(store.live_ids())

    by_key = {i.idempotency_key: i for i in journal.pending() + journal.by_status(IntentStatus.ORPHANED)}
    assert by_key["k-intact"].status is IntentStatus.PENDING
    assert by_key["k-corrupt"].status is IntentStatus.ORPHANED
    assert by_key["k-missing"].status is IntentStatus.ORPHANED
    assert by_key["k-no-checkpoint"].status is IntentStatus.PENDING
