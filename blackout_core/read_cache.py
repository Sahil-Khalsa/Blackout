"""Local read cache with staleness-aware precondition evaluation.

Offline reads are served from here instead of failing outright, and every
cached value's age feeds directly into the authority decision
(docs/blackout-design.md Section 2.7): a precondition evaluated against
stale data is a weak justification, and PolicyEngine refuses rather than
defers when the evidence was already stale at capture time.

Age is computed from a monotonic clock, the same convention as
journal.py::Intent (`created_mono_ns`). Staleness is one of the few things
in this codebase that *branches* on age -- REFUSE vs DEFER hinges on it --
so it may not trust a clock that can drift across an offline session.
"""

from __future__ import annotations

import math
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .policy import PreconditionValue


@dataclass(slots=True)
class CachedRead:
    key: str
    value: Any
    fetched_mono_ns: int
    boot_id: str
    fetched_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    """Wall clock, display-only -- nothing branches on it. See journal.py's
    created_at for the same convention."""

    def age_s(self, now_ns: int) -> float:
        return (now_ns - self.fetched_mono_ns) / 1e9


class ReadCache:
    """In-memory, process-scoped read cache.

    Not yet persisted (STATUS.md). The boot_id stamp is here in anticipation
    of that: a persisted entry surviving into a new process would otherwise
    compare its fetched_mono_ns against a new monotonic epoch and report a
    bogus-fresh age, silently turning a REFUSE into a DEFER -- the same
    failure Intent.is_expired's boot_id check exists to prevent. It's inert
    today since every entry in a given instance shares one boot_id.
    """

    def __init__(self, clock: Callable[[], int] = time.monotonic_ns) -> None:
        self._clock = clock
        self.boot_id = uuid.uuid4().hex
        self._entries: dict[str, CachedRead] = {}

    @property
    def now_ns(self) -> int:
        return self._clock()

    def put(self, key: str, value: Any) -> CachedRead:
        entry = CachedRead(
            key=key, value=value, fetched_mono_ns=self.now_ns, boot_id=self.boot_id
        )
        self._entries[key] = entry
        return entry

    def get(self, key: str) -> CachedRead | None:
        return self._entries.get(key)

    def __contains__(self, key: object) -> bool:
        return key in self._entries


@dataclass(frozen=True, slots=True)
class Precondition:
    """A named, checkable claim about cached data.

    cache_key and predicate are both functions of a tool call's args. A
    precondition and the read tool that feeds it are declared independently
    (e.g. place_restock_order(sku=...) and read_inventory(sku=...)) but must
    derive the same cache key from their respective arg dicts, or the
    precondition will simply never find the read it depends on.
    """

    name: str
    cache_key: Callable[[Mapping[str, Any]], str]
    predicate: Callable[[Any], bool]


class PreconditionRegistry:
    def __init__(self, cache: ReadCache) -> None:
        self.cache = cache
        self._defs: dict[str, Precondition] = {}

    def register(
        self,
        name: str,
        cache_key: Callable[[Mapping[str, Any]], str],
        predicate: Callable[[Any], bool],
    ) -> None:
        self._defs[name] = Precondition(name, cache_key, predicate)

    def evaluate(self, name: str, args: Mapping[str, Any]) -> PreconditionValue:
        defn = self._defs[name]
        entry = self.cache.get(defn.cache_key(args))
        if entry is None or entry.boot_id != self.cache.boot_id:
            # Never fetched (or fetched in a prior boot -- see CachedRead):
            # infinitely stale and unsatisfied, never a justification.
            return PreconditionValue(name=name, value=None, source_age_s=math.inf, satisfied=False)
        return PreconditionValue(
            name=name,
            value=entry.value,
            source_age_s=entry.age_s(self.cache.now_ns),
            satisfied=defn.predicate(entry.value),
        )

    def evaluate_many(
        self, names: Sequence[str], args: Mapping[str, Any]
    ) -> list[PreconditionValue]:
        return [self.evaluate(n, args) for n in names]
