"""Model router: routes tool-call proposals to cloud API, local model, or
deterministic rules, based on the current tier.

Protocols and the tier-3 rules backend are pure and stdlib-only so
blackout_core stays importable -- and the chaos harness runnable -- without
any model backend installed or any API credentials present. Concrete cloud
and local backends live in blackout_core.backends and are only imported when
actually selected.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .policy import Tier, ToolPolicy, ToolRegistry


class BackendUnavailable(RuntimeError):
    """The backend could not be reached at all (network, process down,
    auth failure). Callers should treat this like a failed tier probe --
    demote and retry later -- never as "the model said no"."""


class StructuralFailure(RuntimeError):
    """The backend responded but violated the structural contract: non-JSON
    output, a tool name outside the offered set, or schema-invalid args.

    Per docs/blackout-design.md §2.9, the fallback for a broken constrained
    decode is to demote to tier 3, never to retry with unconstrained output.
    """


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A proposed tool invocation, plus the raw model output it came from.

    `raw` is kept for tracing and for the chaos harness's fabrication
    detector, which diffs what the model said against a call ledger.
    """

    tool: str
    args: dict[str, Any]
    raw: str = ""


class ModelBackend(Protocol):
    def propose(
        self, tools: Sequence[ToolPolicy], tier: Tier, task: str
    ) -> ToolCall | None:
        """Given the tools authorized at this tier and a task description,
        return a proposed call, or None if the backend declines to act.

        Implementations must never propose a tool outside `tools`. For
        model-backed implementations that has to be structural (schema- or
        grammar-constrained generation), not a post-hoc filter -- a filter
        only hides an unauthorized call, it doesn't make one impossible.
        """
        ...


@dataclass(slots=True)
class Rule:
    """A deterministic (task keyword -> tool call) mapping for tier 3.

    No model is involved: matching is plain substring containment, checked
    in order, first match wins. This is deliberately dumb -- tier 3 exists so
    "the model is broken" has a defined, auditable destination rather than an
    unconstrained retry.
    """

    contains: str
    tool: str
    args: Callable[[str], dict[str, Any]]


class RulesBackend:
    """Tier-3 backend: no model, whitelisted paths only."""

    def __init__(self, rules: Sequence[Rule] = ()) -> None:
        self.rules = list(rules)

    def propose(
        self, tools: Sequence[ToolPolicy], tier: Tier, task: str
    ) -> ToolCall | None:
        offered = {t.name for t in tools}
        lowered = task.lower()
        for rule in self.rules:
            if rule.contains in lowered and rule.tool in offered:
                return ToolCall(
                    tool=rule.tool, args=rule.args(task), raw=f"rule:{rule.contains}"
                )
        return None


class ModelRouter:
    """Dispatches a task to a backend by tier.

    Tier 0 (JOURNAL_DOWN) is checked by identity, never by numeric
    comparison against the other tiers -- see policy.py's Tier docstring for
    why JOURNAL_DOWN isn't part of that ordering. At JOURNAL_DOWN no model is
    invoked at all: nothing durable can happen regardless of what's
    proposed, so routing through the rules backend keeps behavior auditable
    rather than trusting a model's output when nothing it proposes can be
    promised.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        cloud: ModelBackend | None = None,
        local: ModelBackend | None = None,
        rules: ModelBackend | None = None,
    ) -> None:
        self.registry = registry
        self.cloud = cloud
        self.local = local
        self.rules = rules

    def propose(self, tier: Tier, task: str) -> ToolCall | None:
        backend = self._backend_for(tier)
        tools = self.registry.available_at(tier)
        return backend.propose(tools, tier, task)

    def _backend_for(self, tier: Tier) -> ModelBackend:
        if tier is Tier.JOURNAL_DOWN:
            return self._require(self.rules, "rules", tier)
        if tier is Tier.CLOUD:
            return self._require(self.cloud, "cloud", tier)
        if tier is Tier.LOCAL:
            return self._require(self.local, "local", tier)
        if tier is Tier.RULES:
            return self._require(self.rules, "rules", tier)
        raise AssertionError(f"unhandled tier {tier!r}")

    @staticmethod
    def _require(backend: ModelBackend | None, name: str, tier: Tier) -> ModelBackend:
        if backend is None:
            raise RuntimeError(f"no {name} backend configured for tier {int(tier)}")
        return backend
