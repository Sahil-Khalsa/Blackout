"""Tool registry and policy engine.

The policy engine answers exactly one question:

    Given this tool, this tier, and these arguments -- may it execute now,
    must it be deferred, or must it be refused outright?

It is deliberately pure and synchronous. It performs no I/O, takes no locks
and never touches the journal. Everything it needs is passed in. That makes
the authority decision trivially testable and means the chaos harness can
assert on decisions without standing up a runtime.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Any


class Tier(IntEnum):
    """Capability tiers, lowest number = most capable.

    JOURNAL_DOWN is tier 0 by name but sits *below* tier 3 in authority: it is
    the state where deferral itself is impossible, so nothing may be promised.
    IntEnum ordering is therefore not meaningful for JOURNAL_DOWN -- always
    check it explicitly rather than comparing.
    """

    CLOUD = 1
    LOCAL = 2
    RULES = 3
    JOURNAL_DOWN = 0


class Effect(StrEnum):
    READ = "read"
    LOCAL_WRITE = "local_write"
    EXTERNAL_WRITE = "external_write"


class OfflinePolicy(StrEnum):
    """What to do when the current tier is insufficient to execute directly."""

    EXECUTE = "execute"  # proceed anyway; only valid for reads
    DEFER = "defer"  # write a durable intent for later approval
    REFUSE = "refuse"  # worthless or harmful if done late


class Decision(StrEnum):
    EXECUTE = "execute"
    DEFER = "defer"
    REFUSE = "refuse"


@dataclass(frozen=True, slots=True)
class PolicyResult:
    decision: Decision
    reason: str
    """Machine-readable reason code. Surfaced in traces and asserted on by the
    harness -- never reformat these for display without keeping the code."""

    detail: str = ""

    def __bool__(self) -> bool:
        return self.decision is Decision.EXECUTE


@dataclass(frozen=True, slots=True)
class PreconditionValue:
    """A precondition evaluation plus the age of the data behind it.

    source_age_s is the age of the *underlying read*, not the age of this
    evaluation. A predicate computed a millisecond ago from a six-hour-old
    cached inventory level has source_age_s == 21600.
    """

    name: str
    value: Any
    source_age_s: float
    satisfied: bool


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    name: str
    effect: Effect
    min_tier: Tier
    offline_policy: OfflinePolicy
    idempotency_key: Callable[[Mapping[str, Any]], str] | None = None
    preconditions: Sequence[str] = ()
    max_precondition_staleness_s: float | None = None
    ttl_seconds: int = 3600
    reversible: bool = False
    tier_descriptions: Mapping[int, str] = field(default_factory=dict)
    cache_key: Callable[[Mapping[str, Any]], str] | None = None
    """Derives a read_cache.ReadCache key from this tool's args. Only valid
    on reads -- the result of executing this tool populates the cache under
    that key, so later preconditions can find it. See read_cache.py."""
    resource_key: Callable[[Mapping[str, Any]], str] | None = None
    """Derives the identity of the thing this tool writes, coarser than
    idempotency_key (e.g. just the SKU, not SKU+window) -- two surviving
    intents whose tools both declare this and whose values match are an
    intra-batch conflict (docs/blackout-design.md §2.6 step 6). Optional:
    a tool that doesn't declare it simply never participates in conflict
    detection. See reconciler.py."""

    def __post_init__(self) -> None:
        if self.effect is not Effect.READ and self.idempotency_key is None:
            raise ValueError(
                f"tool {self.name!r} has effect {self.effect} and must declare "
                "an idempotency_key -- replay is unsafe without one"
            )
        if self.offline_policy is OfflinePolicy.EXECUTE and self.effect is not Effect.READ:
            raise ValueError(
                f"tool {self.name!r} may not use offline_policy=execute with "
                f"effect {self.effect} -- only reads may run past their tier"
            )
        if self.preconditions and self.max_precondition_staleness_s is None:
            raise ValueError(
                f"tool {self.name!r} declares preconditions but no "
                "max_precondition_staleness_s -- stale justifications must "
                "have a defined limit"
            )
        if self.cache_key is not None and self.effect is not Effect.READ:
            raise ValueError(
                f"tool {self.name!r} declares cache_key but has effect "
                f"{self.effect} -- only reads populate the read cache"
            )

    def describe_for(self, tier: Tier) -> str:
        """Per-tier description, falling back to the tier-1 string.

        Tier 2 runs a small model with far less context to spare and measurably
        worse selection accuracy on verbose schemas, so it gets terse variants.
        """
        return self.tier_descriptions.get(
            int(tier), self.tier_descriptions.get(int(Tier.CLOUD), self.name)
        )


@dataclass(slots=True)
class RegisteredTool:
    policy: ToolPolicy
    fn: Callable[..., Any]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def tool(self, **policy_kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            policy_kwargs.setdefault("name", fn.__name__)
            policy = ToolPolicy(**policy_kwargs)
            if policy.name in self._tools:
                raise ValueError(f"tool {policy.name!r} already registered")
            self._tools[policy.name] = RegisteredTool(policy=policy, fn=fn)
            return fn

        return decorator

    def get(self, name: str) -> RegisteredTool:
        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(f"unknown tool {name!r}") from None

    def policy(self, name: str) -> ToolPolicy:
        return self.get(name).policy

    def available_at(self, tier: Tier) -> list[ToolPolicy]:
        """Tools a model at this tier may be *offered*.

        This is the set the tier-2 grammar is compiled from. Tools that would
        always refuse are excluded so the model cannot spend attention on them,
        and so an unauthorized call becomes structurally impossible rather than
        a runtime rejection.
        """
        if tier is Tier.JOURNAL_DOWN:
            return [p for p in self._policies() if p.effect is Effect.READ]
        return [
            p
            for p in self._policies()
            if tier <= p.min_tier or p.offline_policy is not OfflinePolicy.REFUSE
        ]

    def _policies(self) -> list[ToolPolicy]:
        return [rt.policy for rt in self._tools.values()]

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)


class TierResolver:
    """Connectivity to capability tier, with asymmetric thresholds.

    Promotion is expensive and demotion is cheap: an agent that optimistically
    promotes on a flaky link will start a cloud-tier action it cannot finish,
    which is strictly worse than staying at tier 2. So promotion requires N
    consecutive successes; a single hard failure demotes immediately.
    """

    def __init__(
        self,
        promote_after: int = 3,
        latency_budget_s: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.promote_after = promote_after
        self.latency_budget_s = latency_budget_s
        self._clock = clock
        self._consecutive_ok = 0
        self._tier = Tier.LOCAL
        self._journal_ok = True
        self._local_model_ok = True
        self._transitions: list[tuple[float, Tier, str]] = []

    @property
    def tier(self) -> Tier:
        if not self._journal_ok:
            return Tier.JOURNAL_DOWN
        if self._tier is Tier.LOCAL and not self._local_model_ok:
            return Tier.RULES
        return self._tier

    def record_probe(self, ok: bool, latency_s: float | None = None) -> Tier:
        degraded = latency_s is not None and latency_s > self.latency_budget_s
        if ok and not degraded:
            self._consecutive_ok += 1
            if self._consecutive_ok >= self.promote_after and self._tier is not Tier.CLOUD:
                self._set(Tier.CLOUD, "probe_streak")
        else:
            self._consecutive_ok = 0
            if self._tier is not Tier.LOCAL:
                self._set(Tier.LOCAL, "probe_slow" if degraded else "probe_failed")
        return self.tier

    def set_journal_available(self, ok: bool) -> Tier:
        """Journal I/O failure drops to tier 0 -- deferral is impossible, so
        nothing may be promised. Reads still serve."""
        if ok != self._journal_ok:
            self._journal_ok = ok
            self._record_transition("journal_recovered" if ok else "journal_unavailable")
        return self.tier

    def set_local_model_available(self, ok: bool) -> Tier:
        """Local model unavailable falls through to tier 3 rather than retrying
        unconstrained. Tier 3 exists so 'the model is broken' has a destination."""
        if ok != self._local_model_ok:
            self._local_model_ok = ok
            self._record_transition("local_model_recovered" if ok else "local_model_failed")
        return self.tier

    def _set(self, tier: Tier, reason: str) -> None:
        self._tier = tier
        self._record_transition(reason)

    def _record_transition(self, reason: str) -> None:
        self._transitions.append((self._clock(), self.tier, reason))

    @property
    def transitions(self) -> list[tuple[float, Tier, str]]:
        """Tier change log. The silent-degradation detector reads this and
        asserts each entry has a matching user-visible disclosure."""
        return list(self._transitions)


class PolicyEngine:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def evaluate(
        self,
        tool_name: str,
        tier: Tier,
        args: Mapping[str, Any],
        preconditions: Sequence[PreconditionValue] = (),
    ) -> PolicyResult:
        if tool_name not in self.registry:
            return PolicyResult(Decision.REFUSE, "unknown_tool", tool_name)

        policy = self.registry.policy(tool_name)

        # Tier 0: the journal is down, so no promise can be recorded. Reads
        # only, and refuse everything else regardless of its own policy.
        if tier is Tier.JOURNAL_DOWN:
            if policy.effect is Effect.READ:
                return PolicyResult(Decision.EXECUTE, "read_at_tier_zero")
            return PolicyResult(
                Decision.REFUSE,
                "journal_unavailable",
                "cannot defer without a durable journal",
            )

        missing = self._check_preconditions(policy, preconditions)
        if missing is not None:
            return missing

        if tier <= policy.min_tier:
            return PolicyResult(Decision.EXECUTE, "tier_authorized")

        # Below the tool's required tier -- offline_policy decides.
        match policy.offline_policy:
            case OfflinePolicy.EXECUTE:
                return PolicyResult(Decision.EXECUTE, "read_below_tier")
            case OfflinePolicy.REFUSE:
                return PolicyResult(
                    Decision.REFUSE,
                    "refuse_below_tier",
                    f"requires tier {int(policy.min_tier)}, at tier {int(tier)}",
                )
            case OfflinePolicy.DEFER:
                stale = self._check_staleness(policy, preconditions)
                if stale is not None:
                    return stale
                return PolicyResult(
                    Decision.DEFER,
                    "defer_below_tier",
                    f"requires tier {int(policy.min_tier)}, at tier {int(tier)}",
                )

        raise AssertionError(f"unhandled offline_policy {policy.offline_policy}")

    def _check_preconditions(
        self, policy: ToolPolicy, preconditions: Sequence[PreconditionValue]
    ) -> PolicyResult | None:
        supplied = {p.name for p in preconditions}
        absent = [n for n in policy.preconditions if n not in supplied]
        if absent:
            return PolicyResult(
                Decision.REFUSE, "preconditions_not_evaluated", ", ".join(absent)
            )
        unsatisfied = [p.name for p in preconditions if not p.satisfied]
        if unsatisfied:
            return PolicyResult(
                Decision.REFUSE, "precondition_unsatisfied", ", ".join(unsatisfied)
            )
        return None

    def _check_staleness(
        self, policy: ToolPolicy, preconditions: Sequence[PreconditionValue]
    ) -> PolicyResult | None:
        """Refuse rather than defer when the justification was already stale.

        Deferring on evidence that was old at capture time produces an intent
        nobody can responsibly approve four hours later. Better to refuse now,
        loudly, than to queue something unreviewable.
        """
        limit = policy.max_precondition_staleness_s
        if limit is None or not preconditions:
            return None
        worst = max(p.source_age_s for p in preconditions)
        if worst > limit:
            return PolicyResult(
                Decision.REFUSE,
                "precondition_too_stale",
                f"{worst:.0f}s old, limit {limit:.0f}s",
            )
        return None

    def idempotency_key(self, tool_name: str, args: Mapping[str, Any]) -> str | None:
        policy = self.registry.policy(tool_name)
        if policy.idempotency_key is None:
            return None
        return policy.idempotency_key(args)
