# Status

Last updated: 2026-08-18. Full rationale for every item lives in `docs/blackout-design.md`; this
file tracks only what exists versus what doesn't.

## Scaffolding

- [x] Package layout (`blackout_core/`, `blackout_chaos/`, `scripts/`, `docs/`)
- [x] `pyproject.toml`, editable-installable (`pip install -e ".[dev]"`)
- [x] git repo initialized (nothing committed yet)
- [x] `.claude/settings.json`, `CLAUDE.md`
- [ ] CI

## `blackout_core`

Component table mirrors `docs/blackout-design.md` §2.1.

| Component | Status | Notes |
|---|---|---|
| Tool registry | done | `policy.py::ToolRegistry` |
| Policy engine | done | `policy.py::PolicyEngine.evaluate` |
| Tier resolver | done | `policy.py::TierResolver`, asymmetric hysteresis |
| Intent journal | done | `journal.py::IntentJournal`, `Intent` — WAL SQLite, HMAC, monotonic TTL |
| Model router | not started | §2.9 — GBNF-constrained tier-2 decoding, Ollama integration |
| Read cache | not started | §2.7 — offline reads with staleness metadata |
| Loop checkpoint | not started | journal has the *hooks* (`task_checkpoint_id`, `orphan_missing_checkpoints`) but nothing creates/stores a checkpoint yet |
| Reconciler | not started | §2.6 — journal exposes the primitives one needs (`pending`, `duplicates_of`, `by_status`, `resolve`), but the reconcile algorithm itself (expire → topo-sort `depends_on` → collapse duplicates → re-evaluate preconditions → classify ready/ready_with_drift/rejected → detect conflicts) isn't written |
| Approval surface | not started | CLI review of the batch with precondition diffs |
| Trace buffer | not started | OpenTelemetry spans, offline-buffered |

Verification: `scripts/smoke.py` (manual, prints — not pytest). No `tests/` suite exists yet.

## `blackout_chaos`

Not started. Empty placeholder package. Per §3: injection points (7 fault types), scenario spec,
5 detectors, mock backend with effect ledger, and the naive/framework baseline comparison are all
outstanding.

## Known discrepancies vs. design doc

- §4 names Pydantic for the policy schema; the implementation uses stdlib `dataclasses`
  throughout. No pydantic dependency exists. Unresolved decision — migrate or update the doc.

## Next up

Per the build plan (§5), the rest of Week 1: model router with GBNF-constrained tier 2, and a
minimal agent loop wiring the registry + policy engine + tier resolver together — milestone is
pulling the network and watching the agent refuse a tier-1 tool instead of hanging or
hallucinating.
