"""Live walkthrough of the approval inbox (docs/blackout-design.md §7 steps
4-5): reconnect after an offline session and review a batch containing one
ready intent, one flagged with precondition drift, and one auto-rejected as
stale -- exactly the three outcomes the demo calls for.

Run: python scripts/approval_inbox.py
"""

import tempfile
import time
from pathlib import Path

from blackout_core import (
    Effect,
    Intent,
    IntentJournal,
    OfflinePolicy,
    PolicyEngine,
    PreconditionRegistry,
    ReadCache,
    Tier,
    ToolRegistry,
)
from blackout_core.cli import run_interactive

registry = ToolRegistry()


@registry.tool(
    effect=Effect.EXTERNAL_WRITE,
    min_tier=Tier.CLOUD,
    offline_policy=OfflinePolicy.DEFER,
    idempotency_key=lambda a: f"restock:{a['sku']}:{a['window']}",
    preconditions=["inventory.below_threshold"],
    max_precondition_staleness_s=3600,
    ttl_seconds=14400,
    reversible=False,
)
def place_restock_order(sku: str, qty: int, window: str) -> str:
    return f"restock order placed for SKU {sku} ({qty} units, {window})"


class _Clock:
    """Fake monotonic clock so a demo run doesn't need to sleep 4000s to
    show staleness -- the same technique tests/test_loop.py uses."""

    def __init__(self) -> None:
        self.t = time.monotonic_ns()

    def __call__(self) -> int:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += int(seconds * 1e9)


def main() -> None:
    journal_path = Path(tempfile.mkdtemp()) / "journal.db"
    journal = IntentJournal(journal_path)
    clock = _Clock()
    cache = ReadCache(clock)
    preconditions = PreconditionRegistry(cache)
    preconditions.register(
        "inventory.below_threshold",
        cache_key=lambda a: f"inventory:{a['sku']}",
        predicate=lambda v: v["level"] < 10,
    )
    policy = PolicyEngine(registry)

    def defer(sku: str, level: int, window: str) -> Intent:
        cache.put(f"inventory:{sku}", {"sku": sku, "level": level})
        args = {"sku": sku, "qty": 40, "window": window}
        preconditions_now = preconditions.evaluate_many(["inventory.below_threshold"], args)
        decision = policy.evaluate("place_restock_order", Tier.LOCAL, args, preconditions_now)
        assert decision.decision.value == "defer", decision
        return journal.append(
            Intent.from_evaluation(
                tool="place_restock_order",
                args=args,
                idempotency_key=f"restock:{sku}:{window}",
                tier_at_creation=int(Tier.LOCAL),
                ttl_seconds=14400,
                preconditions=preconditions_now,
            )
        )

    print("--- offline session: three restock orders deferred at tier 2 ---")
    defer("100", level=4, window="next-week")  # will stay unchanged -> ready
    defer("200", level=4, window="next-week")  # will change but stay satisfied -> drift
    defer("300", level=4, window="next-week")  # will simply age out -> too stale

    print("--- reconnect: 4000s pass, sku 100 and 200 get fresh reads, sku 300 doesn't ---")
    clock.advance(4000)
    cache.put("inventory:100", {"sku": "100", "level": 4})  # re-read: unchanged
    cache.put("inventory:200", {"sku": "200", "level": 7})  # re-read: changed, still < 10
    # sku 300 is never re-read -- its cached evidence is now 4000s old, past the
    # tool's max_precondition_staleness_s=3600, so it's rejected rather than
    # replayed on justification that was already stale by the time anyone looked.

    print("--- approval inbox ---")
    run_interactive(journal, registry, preconditions)


if __name__ == "__main__":
    main()
