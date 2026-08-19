"""Minimal agent loop: wires the tier resolver, model router, policy engine,
and read cache into a single step function.

If a read_cache and preconditions registry are configured, the loop fetches
a proposed tool's declared preconditions itself (docs/blackout-design.md
§2.7) whenever the caller doesn't supply them explicitly, and any tool with
a `cache_key` that executes as a READ populates the cache for later
precondition lookups. Without them, DEFER still works, but only for tools
whose preconditions the caller supplies up front.

Backend failures feed back into tier resolution per §2.9's stated fallback:
an unreachable backend is a failed probe (demote, don't hang); a structural
failure from the constrained local-model backend (bad JSON, an out-of-set
tool) marks the local model unavailable so the resolver falls through to
tier 3 -- never retried with unconstrained output.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .journal import Intent, IntentJournal, JournalUnavailable
from .policy import Decision, PolicyEngine, PolicyResult, PreconditionValue, Tier, TierResolver
from .read_cache import PreconditionRegistry, ReadCache
from .router import BackendUnavailable, ModelRouter, StructuralFailure, ToolCall


@dataclass(frozen=True, slots=True)
class StepResult:
    tier: Tier
    task: str
    proposal: ToolCall | None
    decision: PolicyResult | None
    result: object | None = None
    intent_id: str | None = None
    error: str | None = None


class AgentLoop:
    def __init__(
        self,
        tier_resolver: TierResolver,
        router: ModelRouter,
        policy: PolicyEngine,
        journal: IntentJournal | None = None,
        read_cache: ReadCache | None = None,
        preconditions: PreconditionRegistry | None = None,
    ) -> None:
        self.tier_resolver = tier_resolver
        self.router = router
        self.policy = policy
        self.journal = journal
        self.read_cache = read_cache
        self.preconditions = preconditions

    def step(
        self,
        task: str,
        preconditions: Sequence[PreconditionValue] = (),
        task_checkpoint_id: str | None = None,
        reasoning_trace_id: str | None = None,
    ) -> StepResult:
        tier = self.tier_resolver.tier

        try:
            proposal = self.router.propose(tier, task)
        except BackendUnavailable as exc:
            self.tier_resolver.record_probe(False)
            return StepResult(tier=tier, task=task, proposal=None, decision=None, error=str(exc))
        except StructuralFailure as exc:
            if tier is Tier.LOCAL:
                self.tier_resolver.set_local_model_available(False)
            return StepResult(tier=tier, task=task, proposal=None, decision=None, error=str(exc))

        if proposal is None:
            return StepResult(tier=tier, task=task, proposal=None, decision=None)

        tool_policy = self.router.registry.policy(proposal.tool)
        if not preconditions and self.preconditions is not None and tool_policy.preconditions:
            preconditions = self.preconditions.evaluate_many(tool_policy.preconditions, proposal.args)

        decision = self.policy.evaluate(proposal.tool, tier, proposal.args, preconditions)

        if decision.decision is Decision.EXECUTE:
            fn = self.router.registry.get(proposal.tool).fn
            result = fn(**proposal.args)
            if self.read_cache is not None and tool_policy.cache_key is not None:
                self.read_cache.put(tool_policy.cache_key(proposal.args), result)
            return StepResult(tier=tier, task=task, proposal=proposal, decision=decision, result=result)

        if decision.decision is Decision.DEFER:
            if self.journal is None:
                raise RuntimeError("policy deferred but no journal is configured")
            idem = (
                tool_policy.idempotency_key(proposal.args)
                if tool_policy.idempotency_key
                else proposal.tool
            )
            try:
                intent = self.journal.append(
                    Intent.from_evaluation(
                        tool=proposal.tool,
                        args=proposal.args,
                        idempotency_key=idem,
                        tier_at_creation=int(tier),
                        ttl_seconds=tool_policy.ttl_seconds,
                        preconditions=preconditions,
                        task_checkpoint_id=task_checkpoint_id,
                        reasoning_trace_id=reasoning_trace_id,
                    )
                )
            except JournalUnavailable:
                self.tier_resolver.set_journal_available(False)
                raise
            return StepResult(
                tier=tier, task=task, proposal=proposal, decision=decision, intent_id=intent.id
            )

        # REFUSE
        return StepResult(tier=tier, task=task, proposal=proposal, decision=decision)
