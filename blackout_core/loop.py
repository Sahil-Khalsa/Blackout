"""Minimal agent loop: wires the tier resolver, model router, and policy
engine into a single step function.

Deliberately thin, and it does not fetch preconditions itself -- that's the
read cache's job (docs/blackout-design.md §2.7, not yet built, see
STATUS.md) -- so DEFER only works for tools whose preconditions the caller
supplies up front. Execution and refusal work fully today regardless.

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
    ) -> None:
        self.tier_resolver = tier_resolver
        self.router = router
        self.policy = policy
        self.journal = journal

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

        decision = self.policy.evaluate(proposal.tool, tier, proposal.args, preconditions)

        if decision.decision is Decision.EXECUTE:
            fn = self.router.registry.get(proposal.tool).fn
            result = fn(**proposal.args)
            return StepResult(tier=tier, task=task, proposal=proposal, decision=decision, result=result)

        if decision.decision is Decision.DEFER:
            if self.journal is None:
                raise RuntimeError("policy deferred but no journal is configured")
            tool_policy = self.router.registry.policy(proposal.tool)
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
