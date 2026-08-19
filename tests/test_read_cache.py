"""ReadCache / PreconditionRegistry, tested directly -- no AgentLoop
involved. Loop-level integration (a real DEFER produced from a populated
cache, and a REFUSE produced from a stale one) lives in test_loop.py.
"""

import math

from blackout_core import PreconditionRegistry, ReadCache


class FakeClock:
    """Monotonic-shaped fake so tests can advance time deterministically
    instead of racing the wall clock."""

    def __init__(self, start_ns: int = 0) -> None:
        self.t = start_ns

    def __call__(self) -> int:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += int(seconds * 1e9)


def test_put_get_roundtrip():
    cache = ReadCache()
    cache.put("inventory:991", {"sku": "991", "level": 4})
    entry = cache.get("inventory:991")
    assert entry is not None
    assert entry.value == {"sku": "991", "level": 4}


def test_get_missing_key_returns_none():
    cache = ReadCache()
    assert cache.get("nope") is None


def test_age_s_advances_with_injected_clock():
    clock = FakeClock()
    cache = ReadCache(clock=clock)
    cache.put("k", "v")
    clock.advance(90)
    entry = cache.get("k")
    assert entry.age_s(cache.now_ns) == 90.0


def test_precondition_evaluates_predicate_against_cached_value():
    cache = ReadCache()
    cache.put("inventory:991", {"level": 4})
    registry = PreconditionRegistry(cache)
    registry.register(
        "inventory.below_threshold",
        cache_key=lambda a: f"inventory:{a['sku']}",
        predicate=lambda v: v["level"] < 10,
    )

    result = registry.evaluate("inventory.below_threshold", {"sku": "991"})
    assert result.satisfied is True
    assert result.value == {"level": 4}
    assert result.source_age_s < 1.0


def test_precondition_never_fetched_is_unsatisfied_and_infinitely_stale():
    cache = ReadCache()
    registry = PreconditionRegistry(cache)
    registry.register(
        "inventory.below_threshold",
        cache_key=lambda a: f"inventory:{a['sku']}",
        predicate=lambda v: v["level"] < 10,
    )

    result = registry.evaluate("inventory.below_threshold", {"sku": "unknown-sku"})
    assert result.satisfied is False
    assert result.source_age_s == math.inf


def test_evaluate_many_returns_one_value_per_name():
    cache = ReadCache()
    cache.put("inventory:991", {"level": 4})
    registry = PreconditionRegistry(cache)
    registry.register(
        "inventory.below_threshold",
        cache_key=lambda a: f"inventory:{a['sku']}",
        predicate=lambda v: v["level"] < 10,
    )
    registry.register(
        "inventory.nonzero",
        cache_key=lambda a: f"inventory:{a['sku']}",
        predicate=lambda v: v["level"] > 0,
    )

    results = registry.evaluate_many(
        ["inventory.below_threshold", "inventory.nonzero"], {"sku": "991"}
    )
    assert [r.name for r in results] == ["inventory.below_threshold", "inventory.nonzero"]
    assert all(r.satisfied for r in results)
