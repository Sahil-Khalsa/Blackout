"""blackout-core: tiered capability runtime with a deferred intent journal.

This package (and everything imported here) is stdlib-only by design, so it
-- and the chaos harness running against it -- work without any model
backend installed or API credentials present. Concrete cloud/local model
backends live in blackout_core.backends and must be imported explicitly.
"""

from .checkpoint import Checkpoint, CheckpointStatus, CheckpointStore
from .journal import Intent, IntentJournal, IntentStatus, JournalUnavailable
from .loop import AgentLoop, StepResult
from .policy import (
    Decision,
    Effect,
    OfflinePolicy,
    PolicyEngine,
    PolicyResult,
    PreconditionValue,
    RegisteredTool,
    Tier,
    TierResolver,
    ToolPolicy,
    ToolRegistry,
)
from .read_cache import CachedRead, Precondition, PreconditionRegistry, ReadCache
from .reconciler import ApprovalBatch, PreconditionDrift, ReadyWithDrift, reconcile
from .router import (
    BackendUnavailable,
    ModelBackend,
    ModelRouter,
    Rule,
    RulesBackend,
    StructuralFailure,
    ToolCall,
)
from .schema import args_schema_for, flat_tool_call_schema, tool_call_schema

__all__ = [
    "AgentLoop",
    "ApprovalBatch",
    "BackendUnavailable",
    "CachedRead",
    "Checkpoint",
    "CheckpointStatus",
    "CheckpointStore",
    "Decision",
    "Effect",
    "Intent",
    "IntentJournal",
    "IntentStatus",
    "JournalUnavailable",
    "ModelBackend",
    "ModelRouter",
    "OfflinePolicy",
    "PolicyEngine",
    "PolicyResult",
    "Precondition",
    "PreconditionDrift",
    "PreconditionRegistry",
    "PreconditionValue",
    "ReadCache",
    "ReadyWithDrift",
    "RegisteredTool",
    "Rule",
    "RulesBackend",
    "StepResult",
    "StructuralFailure",
    "Tier",
    "TierResolver",
    "ToolCall",
    "ToolPolicy",
    "ToolRegistry",
    "args_schema_for",
    "flat_tool_call_schema",
    "reconcile",
    "tool_call_schema",
]
