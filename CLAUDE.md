# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Blackout is an authority runtime for agents that lose connectivity, plus a chaos-testing harness that proves it works. Two components:

- `blackout_core/` — tiered capability runtime with a deferred intent journal (implemented)
- `blackout_chaos/` — network-partition fault injection and behavioral scoring (not yet implemented — see `docs/blackout-design.md` §3)

The full design rationale — including the reasoning behind every field on the tool policy schema — lives in `docs/blackout-design.md`. Treat it as the spec of record; when extending `blackout_core`, match its field-by-field reasoning rather than re-deriving it.

`STATUS.md` tracks what's implemented versus outstanding, component by component. Update it when you finish or start a component — it's the fastest way for the next session to know where things stand without re-deriving it from a diff.

## Commands

```
pip install -e ".[dev,cloud]"    # editable install; cloud extra pulls in openai (tier-1 backend)
python scripts/smoke.py          # manual end-to-end smoke test of policy engine + journal
pytest                           # full suite (31 tests, seconds; slower if Ollama is reachable and its live test runs)
pytest -k "not ollama_integration"   # skip the live-model test explicitly
pytest tests/test_x.py::test_y   # single test
```

Tier-2 tests require Ollama running locally with `qwen2.5:1.5b` pulled (`ollama pull qwen2.5:1.5b`); they self-skip if `localhost:11434` isn't reachable. Tier-1 tests are mocked and need no API key.

## Architecture

### Authority decision flow

The core abstraction is `PolicyEngine.evaluate(tool_name, tier, args, preconditions) -> PolicyResult` in `blackout_core/policy.py`. It's deliberately pure and synchronous — no I/O, no locking, never touches the journal — so the chaos harness can assert on decisions without standing up a runtime. Everything it needs is passed in by the caller.

Decision order inside `evaluate`:
1. `Tier.JOURNAL_DOWN` (tier 0) short-circuits everything: reads execute, all writes refuse. Checked before preconditions or tier comparison — if the journal can't durably record a deferral, nothing may be promised.
2. Missing/unsatisfied preconditions refuse, regardless of tier.
3. If the current tier is capable enough (`tier <= policy.min_tier`), execute.
4. Otherwise `policy.offline_policy` decides: `EXECUTE` (reads only), `REFUSE`, or `DEFER` — and `DEFER` itself refuses if the precondition evidence was already stale at capture time (`_check_staleness`), rather than queuing an intent nobody could responsibly approve later.

### Tiers (`Tier` IntEnum in `policy.py`)

`CLOUD=1`, `LOCAL=2`, `RULES=3`, `JOURNAL_DOWN=0`. Lower number = more capable, *except* `JOURNAL_DOWN`, which is a separate failure axis (durability, not model capability) that sits below all of them in authority. Never compare `JOURNAL_DOWN` using `<=`/`<` against the others — always check it explicitly first (`TierResolver.tier` and `PolicyEngine.evaluate` both do this).

`TierResolver` computes the live tier from connectivity/journal/model-availability probes with asymmetric hysteresis: promotion needs `promote_after` consecutive good probes, demotion happens on a single failure. This is intentional (docs/blackout-design.md §2.3) — optimistic promotion on a flaky link is worse than staying degraded.

### Tool policy invariants (enforced in `ToolPolicy.__post_init__`)

- Any tool with `effect != READ` must declare `idempotency_key` (a pure function of args — never a random UUID; it's what makes replay collapse duplicate deferred intents).
- `offline_policy=EXECUTE` is only valid for `effect=READ` — writes may never silently proceed past their tier.
- Declaring `preconditions` requires `max_precondition_staleness_s` — there's no such thing as an unbounded-staleness deferral.

### Intent journal (`blackout_core/journal.py`)

Append-only SQLite table in WAL mode; `IntentJournal.append`/`resolve` raise `JournalUnavailable` on any I/O failure rather than swallowing it — the caller must escalate to tier 0, never proceed as if the deferral succeeded.

Two conventions span the whole module and are easy to violate accidentally when extending it:

- **Time**: `created_mono_ns` (from `time.monotonic_ns()`) + `ttl_seconds` is the only thing expiry logic uses (`Intent.is_expired`). `created_at` (wall clock) is display-only — nothing branches on it, because offline devices drift. A monotonic stamp from a previous process boot is treated as expired rather than compared (`boot_id` check), since monotonic clocks reset across reboots.
- **Tamper-evidence**: every row is HMAC-signed over its content (`_sign`). Verification is lazy — it only happens on read via `pending()`/`by_status()` — so `verify_all()` must be run on startup and before building an approval batch to catch tampering in rows nothing routinely reads. A mismatch quarantines that single row to `CORRUPT` status rather than failing the whole read; it stays visible via `corrupt_records()` for the approval inbox, deliberately bypassing HMAC verification (a corrupt record fails verification by definition, so routing it through the normal loader would make it disappear silently).

`Intent.id` is a ULID (`_ulid()`), not a UUID4, so sorting by `id` sorts by creation time — replay ordering is free.

### Model router (`blackout_core/router.py`, `schema.py`, `loop.py`, `backends/`)

`ModelRouter.propose(tier, task)` dispatches to a `ModelBackend` by tier and hands it exactly `registry.available_at(tier)` — the offered set, not the full registry. That line is the entire authority boundary at tier 2: a tool the tier isn't allowed to call is structurally absent from the schema, not merely filtered from the result. `tests/test_schema.py` asserts this mechanically; don't weaken it to a post-hoc filter when adding tools or backends.

Like `PolicyEngine.evaluate`, `ModelRouter._backend_for` checks `Tier.JOURNAL_DOWN` by identity, never by numeric comparison — the same tier-0 trap applies here (see Tiers section above). At `JOURNAL_DOWN` no model backend is invoked at all; it always routes to `rules`.

Backend contract (`ModelBackend` protocol): `propose(tools, tier, task) -> ToolCall | None`, raising `BackendUnavailable` (unreachable/network/auth) or `StructuralFailure` (bad JSON, tool name outside the offered set) rather than returning a degraded result. `AgentLoop.step` catches both and feeds them back into `TierResolver` — `BackendUnavailable` records a failed probe, `StructuralFailure` at tier 2 marks the local model unavailable so the resolver falls through to tier 3. This is the runtime version of §2.9's stated fallback: never retry a broken constrained decode with unconstrained output.

Three backends: `RulesBackend` (tier 3, deterministic substring match, stdlib, also used in tests as a fast stand-in for tiers 1/2), `backends/ollama_backend.py::OllamaBackend` (tier 2, JSON-Schema-constrained generation via Ollama's `format` field — empirically verified to enforce a `oneOf`/`const` discriminated union, see STATUS.md), `backends/openai_backend.py::OpenAIBackend` (tier 1, native OpenAI tool-calling, *not* schema-constrained the same way — an out-of-set tool name is caught as a runtime `StructuralFailure`, not prevented structurally). The two concrete backends are never imported by `blackout_core/__init__.py` or `backends/__init__.py`, so importing `blackout_core` — and running the chaos harness — needs neither `openai` installed nor any credentials. Import the specific backend module to use one.

Per-tool argument schemas are derived from the tool function's type annotations (`schema.py::args_schema_for`; str/int/float/bool only), not stored on `ToolPolicy` — a tool registered without full annotations fails loudly at schema-build time.

### Read cache (`blackout_core/read_cache.py`)

`ReadCache` is an in-memory, process-scoped `dict[str, CachedRead]`; `PreconditionRegistry` maps a named precondition to a `cache_key(args)` function and a `predicate(value)` function, both derived from the *calling tool's* args, and evaluates them into `PreconditionValue`s the same shape `PolicyEngine` already expects from a caller. A `ToolPolicy` may declare `cache_key` (only valid for `effect=READ`); when `AgentLoop.step()` executes such a tool it writes the result into the configured `ReadCache`, and for any proposal whose tool declares `preconditions`, it now auto-evaluates them from the cache when the caller didn't supply any explicitly — so `DEFER` no longer requires the caller to hand-supply preconditions.

Staleness (`CachedRead.age_s`) is computed from `time.monotonic_ns()`, not wall clock, despite §2.7's literal phrasing — consistent with the journal's monotonic-only rule above, because staleness is exactly the kind of elapsed-time logic that *branches* (REFUSE vs DEFER) and so may not trust a clock that can drift offline. `CachedRead` also carries a `boot_id` (inert today since the cache doesn't persist across restarts) so a persisted entry from a prior boot can't later report a bogus-fresh age.

### Loop checkpoints (`blackout_core/checkpoint.py`)

`CheckpointStore` is deliberately shaped like `IntentJournal`: its own SQLite connection pointed at the *same file path* the journal uses (WAL mode allows multiple connections to one file), its own `checkpoints` table, HMAC-signed rows with the same lazy-verify-on-read pattern — but no monotonic-clock/TTL logic, since unlike intents, checkpoints don't expire.

`Checkpoint` content (`task`, `reasoning_trace_id`, `completed_steps: list[dict]`, `pending_plan: dict`) is deliberately opaque and caller-driven rather than a new typed plan structure — `AgentLoop.step()` is still single-shot with no internal notion of a multi-step plan, so there was nothing concrete to type. `step()` already threads `task_checkpoint_id`/`reasoning_trace_id` through to `Intent`, so `loop.py` needed zero changes: a caller calls `store.start(...)` before a task, passes `checkpoint.id` into `step()`, and calls `record_step`/`set_pending_plan`/`complete`/`abandon` around it independently.

`store.live_ids()` returns every checkpoint that loads and HMAC-verifies, regardless of status — completion doesn't erase the record. A checkpoint that fails verification (or was never written) is simply absent from that set, which is what makes `journal.orphan_missing_checkpoints(store.live_ids())` implement the doc's "missing or corrupt → orphaned" rule directly, with no separate quarantine table the way the journal's `CORRUPT` status needs one.

### Known gap between design doc and implementation

`docs/blackout-design.md` §4 names Pydantic as the policy-schema library; the actual implementation uses stdlib `dataclasses` (`@dataclass(frozen=True, slots=True)`) throughout `policy.py` and `journal.py`, and there is no pydantic dependency in `pyproject.toml`. Follow the code, not that line of the doc, unless asked to migrate.

The tier-1 cloud backend is OpenAI (`backends/openai_backend.py`), not Anthropic — a deliberate choice, not an oversight. Doesn't affect the architecture, just the concrete SDK/exception types if extending it.
