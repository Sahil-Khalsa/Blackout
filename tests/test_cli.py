"""Approval-surface CLI (docs/blackout-design.md §2.6 line 40, §7 demo step 5):
a minimal review surface over reconcile()'s ApprovalBatch, plus the two
sections the reconciler itself doesn't produce -- orphaned intents (§2.8)
and corrupt records (§2.11) -- both of which the doc says belong in the
approval inbox.

Split into a testable core (build_inbox / format_inbox / approve / reject)
and a thin interactive shell (run_interactive) with input/output injected,
so the review loop itself can be driven without real stdin/stdout.
"""

import sqlite3

import pytest

from blackout_core import (
    Effect,
    Intent,
    IntentJournal,
    IntentStatus,
    OfflinePolicy,
    PreconditionRegistry,
    PreconditionValue,
    ReadCache,
    Tier,
    ToolRegistry,
)
from blackout_core.cli import InboxView, approve, build_inbox, format_inbox, reject, run_interactive


def _cache_and_preconditions() -> tuple[ReadCache, PreconditionRegistry]:
    cache = ReadCache()
    preconditions = PreconditionRegistry(cache)
    preconditions.register(
        "inventory.below_threshold",
        cache_key=lambda a: f"inventory:{a['sku']}",
        predicate=lambda v: v["level"] < 10,
    )
    return cache, preconditions


def test_build_inbox_on_empty_journal_is_all_empty(registry, tmp_path):
    journal = IntentJournal(tmp_path / "journal.db")
    _, preconditions = _cache_and_preconditions()

    view = build_inbox(journal, registry, preconditions)

    assert view.batch.ready == []
    assert view.batch.ready_with_drift == []
    assert view.orphaned == []
    assert view.corrupt == []


def test_build_inbox_surfaces_ready_intent_from_reconcile(registry, tmp_path):
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

    view = build_inbox(journal, registry, preconditions)

    assert [i.id for i in view.batch.ready] == [intent.id]


def test_build_inbox_surfaces_orphaned_intents(registry, tmp_path):
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
    journal.resolve(intent.id, IntentStatus.ORPHANED, "task checkpoint lost")

    view = build_inbox(journal, registry, preconditions)

    assert [i.id for i in view.orphaned] == [intent.id]
    assert view.batch.ready == []


def test_build_inbox_surfaces_corrupt_records(registry, tmp_path):
    path = tmp_path / "journal.db"
    journal = IntentJournal(path)
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
    raw = sqlite3.connect(path)
    raw.execute("UPDATE intents SET tool = 'tampered' WHERE id = ?", (intent.id,))
    raw.commit()
    raw.close()

    view = build_inbox(journal, registry, preconditions)

    assert [r["id"] for r in view.corrupt] == [intent.id]
    assert view.batch.ready == []


def test_format_inbox_shows_ready_intent_tool_and_id(registry, tmp_path):
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

    view = build_inbox(journal, registry, preconditions)
    text = format_inbox(view, registry)

    assert intent.id in text
    assert "page_oncall" in text


def test_format_inbox_shows_precondition_drift_diff(registry, tmp_path):
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

    view = build_inbox(journal, registry, preconditions)
    text = format_inbox(view, registry)

    assert intent.id in text
    assert "inventory.below_threshold" in text
    assert "'level': 4" in text
    assert "'level': 6" in text


def test_format_inbox_shows_rejected_intent_with_reason(registry, tmp_path):
    """§7 step 5: the demo batch has one intent auto-rejected as stale, and
    the inbox must show it, not just silently drop it -- a rejection nobody
    can see is indistinguishable from lost work."""
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

    view = build_inbox(journal, registry, preconditions)
    text = format_inbox(view, registry)

    assert intent.id in text
    assert "precondition_unsatisfied" in text


def test_format_inbox_tags_irreversible_tool(registry, tmp_path):
    journal = IntentJournal(tmp_path / "journal.db")
    _, preconditions = _cache_and_preconditions()

    journal.append(
        Intent.from_evaluation(
            tool="page_oncall",  # reversible=False (the conftest default)
            args={"oncall": "sre-jane"},
            idempotency_key="page:sre-jane",
            tier_at_creation=2,
            ttl_seconds=3600,
        )
    )

    view = build_inbox(journal, registry, preconditions)
    text = format_inbox(view, registry)

    assert "IRREVERSIBLE" in text


def test_format_inbox_does_not_tag_reversible_tool(tmp_path):
    reg = ToolRegistry()

    @reg.tool(
        effect=Effect.LOCAL_WRITE,
        min_tier=Tier.RULES,
        offline_policy=OfflinePolicy.DEFER,
        idempotency_key=lambda a: f"note:{a['text']}",
        reversible=True,
    )
    def add_note(text: str) -> None:
        return None

    journal = IntentJournal(tmp_path / "journal.db")
    _, preconditions = _cache_and_preconditions()

    journal.append(
        Intent.from_evaluation(
            tool="add_note",
            args={"text": "hello"},
            idempotency_key="note:hello",
            tier_at_creation=2,
            ttl_seconds=3600,
        )
    )

    view = build_inbox(journal, reg, preconditions)
    text = format_inbox(view, reg)

    assert "IRREVERSIBLE" not in text


def test_format_inbox_flags_orphaned_intents(registry, tmp_path):
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
    journal.resolve(intent.id, IntentStatus.ORPHANED, "task checkpoint lost")

    view = build_inbox(journal, registry, preconditions)
    text = format_inbox(view, registry)

    assert intent.id in text
    assert "no checkpoint context" in text.lower()


def test_format_inbox_marks_corrupt_records_unreviewable(registry, tmp_path):
    path = tmp_path / "journal.db"
    journal = IntentJournal(path)
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
    raw = sqlite3.connect(path)
    raw.execute("UPDATE intents SET tool = 'tampered' WHERE id = ?", (intent.id,))
    raw.commit()
    raw.close()

    view = build_inbox(journal, registry, preconditions)
    text = format_inbox(view, registry)

    assert intent.id in text
    assert "UNREVIEWABLE" in text


def test_approve_executes_tool_and_marks_replayed(tmp_path):
    calls = []
    reg = ToolRegistry()

    @reg.tool(
        effect=Effect.EXTERNAL_WRITE,
        min_tier=Tier.CLOUD,
        offline_policy=OfflinePolicy.DEFER,
        idempotency_key=lambda a: f"page:{a['oncall']}",
    )
    def page_oncall(oncall: str) -> str:
        calls.append(oncall)
        return f"paged {oncall}"

    journal = IntentJournal(tmp_path / "journal.db")
    intent = journal.append(
        Intent.from_evaluation(
            tool="page_oncall",
            args={"oncall": "sre-jane"},
            idempotency_key="page:sre-jane",
            tier_at_creation=2,
            ttl_seconds=3600,
        )
    )

    result = approve(journal, reg, intent)

    assert result == "paged sre-jane"
    assert calls == ["sre-jane"]
    assert journal.by_status(IntentStatus.REPLAYED)[0].id == intent.id


def test_approve_propagates_exception_and_leaves_intent_pending(tmp_path):
    reg = ToolRegistry()

    @reg.tool(
        effect=Effect.EXTERNAL_WRITE,
        min_tier=Tier.CLOUD,
        offline_policy=OfflinePolicy.DEFER,
        idempotency_key=lambda a: f"page:{a['oncall']}",
    )
    def page_oncall(oncall: str) -> None:
        raise RuntimeError("paging service down")

    journal = IntentJournal(tmp_path / "journal.db")
    intent = journal.append(
        Intent.from_evaluation(
            tool="page_oncall",
            args={"oncall": "sre-jane"},
            idempotency_key="page:sre-jane",
            tier_at_creation=2,
            ttl_seconds=3600,
        )
    )

    with pytest.raises(RuntimeError):
        approve(journal, reg, intent)

    assert journal.pending()[0].id == intent.id


def test_reject_marks_intent_rejected_with_note(registry, tmp_path):
    journal = IntentJournal(tmp_path / "journal.db")

    intent = journal.append(
        Intent.from_evaluation(
            tool="page_oncall",
            args={"oncall": "sre-jane"},
            idempotency_key="page:sre-jane",
            tier_at_creation=2,
            ttl_seconds=3600,
        )
    )

    reject(journal, intent, "on-call already paged manually")

    resolved = journal.by_status(IntentStatus.REJECTED)[0]
    assert resolved.id == intent.id
    assert resolved.resolution_note == "on-call already paged manually"


def test_run_interactive_approve_choice_executes_and_marks_replayed(tmp_path):
    calls = []
    reg = ToolRegistry()

    @reg.tool(
        effect=Effect.EXTERNAL_WRITE,
        min_tier=Tier.CLOUD,
        offline_policy=OfflinePolicy.DEFER,
        idempotency_key=lambda a: f"page:{a['oncall']}",
    )
    def page_oncall(oncall: str) -> str:
        calls.append(oncall)
        return f"paged {oncall}"

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

    responses = iter(["a"])
    run_interactive(
        journal, reg, preconditions, input_fn=lambda prompt: next(responses), output_fn=lambda _: None
    )

    assert calls == ["sre-jane"]
    assert journal.by_status(IntentStatus.REPLAYED)[0].id == intent.id


def test_run_interactive_reject_choice_marks_rejected_without_executing(tmp_path):
    calls = []
    reg = ToolRegistry()

    @reg.tool(
        effect=Effect.EXTERNAL_WRITE,
        min_tier=Tier.CLOUD,
        offline_policy=OfflinePolicy.DEFER,
        idempotency_key=lambda a: f"page:{a['oncall']}",
    )
    def page_oncall(oncall: str) -> None:
        calls.append(oncall)

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

    responses = iter(["r"])
    run_interactive(
        journal, reg, preconditions, input_fn=lambda prompt: next(responses), output_fn=lambda _: None
    )

    assert calls == []
    assert journal.by_status(IntentStatus.REJECTED)[0].id == intent.id


def test_run_interactive_skip_choice_leaves_intent_pending(tmp_path):
    calls = []
    reg = ToolRegistry()

    @reg.tool(
        effect=Effect.EXTERNAL_WRITE,
        min_tier=Tier.CLOUD,
        offline_policy=OfflinePolicy.DEFER,
        idempotency_key=lambda a: f"page:{a['oncall']}",
    )
    def page_oncall(oncall: str) -> None:
        calls.append(oncall)

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

    responses = iter(["s"])
    run_interactive(
        journal, reg, preconditions, input_fn=lambda prompt: next(responses), output_fn=lambda _: None
    )

    assert calls == []
    assert journal.pending()[0].id == intent.id


def test_run_interactive_reprompts_on_invalid_choice(tmp_path):
    calls = []
    reg = ToolRegistry()

    @reg.tool(
        effect=Effect.EXTERNAL_WRITE,
        min_tier=Tier.CLOUD,
        offline_policy=OfflinePolicy.DEFER,
        idempotency_key=lambda a: f"page:{a['oncall']}",
    )
    def page_oncall(oncall: str) -> None:
        calls.append(oncall)

    journal = IntentJournal(tmp_path / "journal.db")
    _, preconditions = _cache_and_preconditions()
    journal.append(
        Intent.from_evaluation(
            tool="page_oncall",
            args={"oncall": "sre-jane"},
            idempotency_key="page:sre-jane",
            tier_at_creation=2,
            ttl_seconds=3600,
        )
    )

    responses = iter(["banana", "a"])
    outputs = []
    run_interactive(
        journal, reg, preconditions, input_fn=lambda prompt: next(responses), output_fn=outputs.append
    )

    assert calls == ["sre-jane"]
    assert any("please enter a, r, or s" in line for line in outputs)


def test_run_interactive_never_prompts_for_already_rejected_intents(registry, tmp_path):
    journal = IntentJournal(tmp_path / "journal.db")
    cache, preconditions = _cache_and_preconditions()
    cache.put("inventory:991", {"sku": "991", "level": 15})  # already restocked -> rejected

    journal.append(
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

    def _no_input(prompt: str) -> str:
        raise AssertionError("should never prompt for a rejected intent")

    run_interactive(journal, registry, preconditions, input_fn=_no_input, output_fn=lambda _: None)
