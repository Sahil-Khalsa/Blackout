# Status

Last updated: 2026-08-19 (later same session). Full rationale for every item lives in
`docs/blackout-design.md`; this file tracks only what exists versus what doesn't.

## Scaffolding

- [x] Package layout (`blackout_core/`, `blackout_chaos/`, `scripts/`, `docs/`, `tests/`)
- [x] `pyproject.toml`, editable-installable (`pip install -e ".[dev]"`); `cloud` extra adds `openai`
- [x] git repo initialized, pushed to `github.com/Sahil-Khalsa/Blackout` (`main`)
- [x] `.claude/settings.json`, `CLAUDE.md`
- [x] `.env.example` / `.env` gitignored
- [ ] CI

## `blackout_core`

Component table mirrors `docs/blackout-design.md` §2.1.

| Component | Status | Notes |
|---|---|---|
| Tool registry | done | `policy.py::ToolRegistry` |
| Policy engine | done | `policy.py::PolicyEngine.evaluate` |
| Tier resolver | done | `policy.py::TierResolver`, asymmetric hysteresis |
| Intent journal | done | `journal.py::IntentJournal`, `Intent` — WAL SQLite, HMAC, monotonic TTL |
| Model router | done (Week 1 scope) | `router.py::ModelRouter` + `schema.py` + `backends/`. See below. |
| Minimal agent loop | done, now with real DEFER | `loop.py::AgentLoop`. See below. |
| Read cache | done | `read_cache.py::ReadCache` + `PreconditionRegistry`. See below. |
| Loop checkpoint | done | `checkpoint.py::CheckpointStore` + `Checkpoint`. See below. |
| Reconciler | done | `reconciler.py::reconcile`. See below. |
| Approval surface | done | `cli.py`. See below. |
| Trace buffer | not started | OpenTelemetry spans, offline-buffered |

### Model router detail

- **Tier 1 (cloud)** — `backends/openai_backend.py::OpenAIBackend`. Native OpenAI tool-calling
  (design doc named Anthropic-agnostic "cloud API"; OpenAI was the user's call). Not
  schema-constrained the way tier 2 is — an out-of-set tool name is checked and rejected as a
  `StructuralFailure`, a runtime check, not a structural impossibility. Requires the `cloud` extra
  and `OPENAI_API_KEY`; unit-tested against a mocked client (`tests/test_backends_openai.py`), no
  live key needed for tests.
- **Tier 2 (local)** — `backends/ollama_backend.py::OllamaBackend`. Uses Ollama's `format` field
  (JSON-Schema-constrained sampling) rather than hand-rolled GBNF — the design doc names both as
  equivalent (§2.9). Empirically verified against Ollama 0.32.13 + `qwen2.5:1.5b` that a `oneOf`
  discriminated union (`const` tool name per variant) structurally excludes tools outside the
  union: asked directly to page the on-call engineer with `page_oncall` excluded from the schema,
  the model was constrained into `read_inventory` and could not name the excluded tool (it *did*
  try to smuggle the paging intent into a string argument — content leaked, the tool call itself
  didn't). Regression-tested live in `tests/test_ollama_integration.py` (skips if Ollama isn't
  reachable). `schema.py` also provides `flat_tool_call_schema` (enum + generic args object) as a
  documented fallback if a different model/decoder doesn't honor `oneOf` — not currently used,
  since the union held.
- **Tier 3 (rules)** — `router.py::RulesBackend`. Deterministic substring-match, no model. Used in
  tests as a fast stand-in for tiers 1/2 as well, since it's just another `ModelBackend`.
- Grammar/schema is always compiled from `registry.available_at(tier)` (the offered set), not the
  full registry — `tests/test_schema.py` asserts a `min_tier=CLOUD, offline_policy=REFUSE` tool
  never appears in the tier-2 schema. This is the mechanical proof behind the "physically cannot
  emit an unauthorized call" claim.
- `ToolPolicy` doesn't carry an explicit args JSON Schema; `schema.py::args_schema_for` derives one
  from the tool function's type annotations (str/int/float/bool only), failing loudly if any
  parameter is unannotated rather than guessing a permissive type.
- Backends live under `blackout_core/backends/` and are **not** re-exported from
  `blackout_core/__init__.py` or `backends/__init__.py` — importing `blackout_core` itself (and
  running the chaos harness against it) requires no third-party package and no credentials. Only
  explicitly importing `blackout_core.backends.openai_backend` pulls in `openai`.

### Read cache detail

`read_cache.py::ReadCache` — an in-memory, process-scoped `dict[str, CachedRead]`. Every entry
carries `fetched_mono_ns` (monotonic, matching `journal.py::Intent`'s convention) and a `boot_id`.
`PreconditionRegistry` maps a named precondition (e.g. `"inventory.below_threshold"`) to a
`cache_key(args)` function and a `predicate(value)` function, both derived from the *tool call's*
args — a read tool and the precondition that depends on its output must independently derive the
same cache key from their own (different) arg dicts. `evaluate`/`evaluate_many` produce
`PreconditionValue`s exactly like a caller-supplied one, so `PolicyEngine` doesn't know or care
whether the evidence came from the cache or from a test double.

A `ToolPolicy` can now declare `cache_key` (only valid on `effect=READ`); when `AgentLoop` executes
such a tool, it writes the result into the configured `ReadCache` under that key.

**Deviation from §2.7's literal wording:** the section says cached values carry "the wall-clock age
of the fetch." Implemented with `time.monotonic_ns()` instead, matching the codebase-wide rule in
§2.10 that anything which *branches* on elapsed time may not trust a clock that can drift across an
offline session — and staleness is exactly that: it's the one thing separating a `REFUSE` from a
`DEFER`. Not logged as a discrepancy below; read as informal phrasing for "how old the data is,"
not a clock-source mandate, and the codebase has no wall-clock-driven branch anywhere else to be
consistent with.

`CachedRead.boot_id` is currently inert (the cache is in-memory, so every entry in a process shares
one boot_id) but is there ahead of persistence landing — a persisted entry surviving into a new
process would otherwise report a bogus-fresh age against the new monotonic epoch, silently turning
a `REFUSE` into a `DEFER`.

### Agent loop detail

`loop.py::AgentLoop.step()` reads the current tier from its `TierResolver`, asks the `ModelRouter`
to propose a call, evaluates it through `PolicyEngine`, and executes/defers/refuses. Backend
failures feed back into tier resolution per §2.9's stated fallback: `BackendUnavailable` records a
failed probe; a `StructuralFailure` from the tier-2 backend marks the local model unavailable
(forces the resolver to tier 3) rather than retrying unconstrained.

If constructed with a `read_cache` and `preconditions` registry, `step()` now fetches a proposed
tool's declared preconditions itself whenever the caller doesn't supply them explicitly — closing
the gap flagged here previously. `DEFER` still also works with caller-supplied preconditions (used
by tests that want to inject a precondition without a real cache). The Week 2 milestone —
`place_restock_order` producing a real `DEFER` from a genuine cached precondition snapshot, and
`REFUSE`ing instead when that evidence is stale — is covered by
`tests/test_loop.py::test_defer_uses_precondition_from_populated_read_cache` and
`test_defer_refuses_when_cached_precondition_is_too_stale`.

### Reconciler detail

`reconciler.py::reconcile(journal, registry, preconditions, now_ns=None) -> ApprovalBatch` — the
§2.6 seven-step algorithm, run on reconnect. `ready` and `ready_with_drift` are batch-only
classifications, not journal statuses: those intents stay `PENDING` (they still need human approval
via the not-yet-built CLI). `rejected`/`expired`/`collapsed`/`conflicted` are terminal outcomes
written back via `journal.resolve()` as reconciliation proceeds — `IntentStatus` already carried
these members unused before this component, which was the tell that the journal had been built
anticipating it.

Two ordering questions the doc leaves implicit, resolved:

- **"Collapse duplicates, keep earliest" (step 4)** means earliest by ULID/creation order, not
  position in the step-3 topological sort — dedup groups the ULID-ordered `pending()` list directly,
  so "earliest" never depends on how dependency edges happened to reshuffle the walk.
- **Steps 3 ("drop descendants of rejected parents") and 5 ("classify") are fused**, walked once in
  topological order. That fusion is *why* the doc calls for a topological sort at all — a simple
  filter over pre-existing statuses wouldn't need one. Walking parents before children means a
  parent that goes dead earlier in this same run (rejected by step 5, or expired by step 2) is
  already in the `dead_ids` set by the time its child is classified, so a same-run cascade works,
  not just a cascade from a previous reconcile cycle. A dependency cycle (shouldn't happen) falls
  back to appending the leftover nodes in original order rather than raising.

Step 5 re-evaluates preconditions via the *same* `PreconditionRegistry.evaluate_many` the agent loop
uses, against live cache state — no special-casing for "never re-fetched," since its existing
behavior (`source_age_s=inf, satisfied=False` on a cache miss) already produces the correct
`rejected` outcome for free. `max_precondition_staleness_s` is re-applied to the *fresh* reading at
reconcile time too, not just at capture time — a reconnect that returns a still-stale read isn't a
real justification either (an explicit extension of §2.7's staleness rule beyond its literal
capture-time wording, not a discrepancy from it).

Step 6 (conflict detection, "two intents writing the same resource") needed a "resource" concept
that didn't exist anywhere in the codebase. Added `ToolPolicy.resource_key` — a pure function of
args, optional, mirroring the existing `idempotency_key`/`cache_key` pattern exactly. A tool that
doesn't declare it simply never participates in conflict detection; `place_restock_order` in
`tests/conftest.py` now declares `resource_key=lambda a: f"sku:{a['sku']}"` so two restock orders
for the same SKU but different windows (different `idempotency_key`, so not collapsed as duplicates
in step 4) correctly conflict.

Step 7 (§2.7): `ready` and `ready_with_drift` are each sorted by `Intent.max_source_age_s`
descending — the shakiest justification surfaces first in the approval batch.

Built via TDD, 12 new tests (`tests/test_reconciler.py`) covering all seven steps plus the §2.7 sort.
Two of the twelve (expiry exclusion, and a healthy same-batch dependency staying ready) passed on
first write without an implementation change — they exercise composition of already-correct
primitives (`journal.pending()`'s expiry side effect; a live/ready parent never entering `dead_ids`)
rather than driving new logic, which is expected and was left in as regression coverage rather than
discarded.

### Approval surface detail

`cli.py` — a minimal review surface over `reconciler.reconcile()`'s `ApprovalBatch` (§2.6 line 40,
§7 demo step 5), split into a testable core and a thin interactive shell so the review loop can be
driven without real stdin/stdout:

- `build_inbox(journal, registry, preconditions) -> InboxView` bundles the `ApprovalBatch` with the
  two sections the reconciler itself doesn't produce but the doc says belong in the inbox: orphaned
  intents (§2.8 — `journal.by_status(ORPHANED)`) and corrupt records (§2.11 —
  `journal.corrupt_records()`, genuinely unreviewable).
- `format_inbox(view, registry) -> str` renders every section — ready / ready-with-drift (with the
  diff) / rejected / collapsed / conflicts / orphaned / corrupt-unreviewable — tagging any intent
  whose tool has `reversible=False` with an `IRREVERSIBLE` warning (the field exists per line 101
  specifically to make the surface warn harder here).
- `approve(journal, registry, intent)` **executes** the deferred tool call
  (`registry.get(tool).fn(**args)`) and only then marks the intent `REPLAYED` — `REPLAYED` existing
  as a terminal `IntentStatus` is what makes "approve" mean something concrete rather than just
  flipping a label. If the tool raises, the exception propagates and the intent stays `PENDING`
  rather than being marked replayed on a failed effect. `reject(journal, intent, note)` marks
  `REJECTED`.
- `run_interactive(journal, registry, preconditions, input_fn=input, output_fn=print)` is the actual
  per-item approve/reject/skip loop, with I/O injected for testing. Only intents still awaiting a
  human decision are prompted — ready, ready_with_drift, and orphaned (shown but flagged as lacking
  checkpoint context); rejected/expired/collapsed/conflicted are already terminal and corrupt records
  are never actionable, so none of those consume a response.

`scripts/approval_inbox.py` is a runnable walkthrough of §7 steps 4-5: three restock intents deferred
offline, then a simulated reconnect (via the same injectable-clock technique `test_loop.py` uses, so
the demo doesn't need to sleep 4000 real seconds) producing exactly the batch the doc's demo script
describes — one `ready`, one `ready_with_drift` with a visible diff, one auto-rejected as stale.
Running it live caught a real bug before commit: `format_inbox` initially never rendered the
rejected/collapsed/conflicts sections at all, so the "auto-rejected as stale" intent the doc's demo
calls for was silently invisible in the inbox — fixed via a new test
(`test_format_inbox_shows_rejected_intent_with_reason`) before shipping. It also caught a Windows
console encoding crash (`cp1252` can't encode the `⚠` character used in the irreversible warning),
fixed by using a plain `!!` marker instead.

Built via TDD, 19 new tests (`tests/test_cli.py`). Several (the empty-inbox case, orphaned/corrupt
surfacing in `build_inbox`, the reversible-tool negative case, exception-propagation, and three of
the four `run_interactive` behaviors) passed on first write without an implementation change, for
the same reason as in the reconciler: they exercise composition of already-correct primitives or
behavior that fell out of code written for an adjacent test, not new logic — left in as regression
coverage.

### Loop checkpoint detail

`checkpoint.py::CheckpointStore` (§2.8) — its own SQLite connection pointed at the *same file path*
`IntentJournal` uses (WAL mode supports multiple connections to one file), its own `checkpoints`
table, HMAC-signed rows with the same lazy-verify-on-read pattern as the journal. Deliberately
self-contained rather than reaching into `IntentJournal`'s internals — independently constructible
and testable, same shape as `IntentJournal` itself.

`Checkpoint` content is intentionally opaque/caller-driven (`task`, `reasoning_trace_id`,
`completed_steps: list[dict]`, `pending_plan: dict`) rather than a new typed multi-step-plan
structure — `AgentLoop.step()` is still single-shot with no internal notion of a plan, and inventing
one wasn't needed: `step()` already threads `task_checkpoint_id`/`reasoning_trace_id` through to
`Intent`, so a caller just calls `store.start(...)` before a task, passes `checkpoint.id` into
`step()`, and calls `record_step`/`complete` around it. **`loop.py` did not need to change at all.**

A checkpoint failing HMAC verification (or never having been written) simply isn't in
`store.live_ids()` — which is exactly the doc's "missing or corrupt → orphaned" rule with no
separate quarantine table needed, unlike the journal's `CORRUPT` status (there's no approval-inbox
need yet to *see* a broken checkpoint, only to know it's not live). No monotonic-clock/TTL logic:
checkpoints don't expire, so unlike the journal there's nothing here that branches on elapsed time —
`created_at` is wall-clock and purely for display, with no monotonic counterpart at all.

`journal.orphan_missing_checkpoints(checkpoint_store.live_ids())` is the restart-recovery call — a
one-liner a future bootstrap/CLI invokes on startup. Proven end-to-end in
`tests/test_checkpoint.py::test_orphan_missing_checkpoints_flags_intents_whose_checkpoint_is_missing_or_corrupt`:
an intent referencing an intact checkpoint stays `PENDING`; one referencing a corrupted or
never-written checkpoint becomes `ORPHANED`; one with no checkpoint at all (never had a plan to
lose) is untouched.

Built via TDD (`superpowers:test-driven-development`): every method has a test written and watched
red before the minimal implementation went green.

Verification: `scripts/smoke.py` (manual, policy engine + journal only), `scripts/approval_inbox.py`
(manual, live approval-inbox walkthrough), plus a real `tests/` suite — 62 tests, `pytest` (~12s
without Ollama reachable; the live Ollama integration test self-skips when it isn't).

## `blackout_chaos`

Not started. Empty placeholder package. Per §3: injection points (7 fault types), scenario spec,
5 detectors, mock backend with effect ledger, and the naive/framework baseline comparison are all
outstanding.

## Known discrepancies vs. design doc

- §4 names Pydantic for the policy schema; the implementation uses stdlib `dataclasses`
  throughout. No pydantic dependency exists. Unresolved decision — migrate or update the doc.
- §4 names Ollama generically; tier-1 cloud is OpenAI, not the Anthropic/other API implied
  elsewhere. Doesn't affect the architecture, just the concrete SDK.

## Next up

Week 2 (§5) is complete. `scripts/approval_inbox.py` covers §7 steps 4-5 (reconnect → approval
batch) as a live, runnable walkthrough; steps 1-3 (agent working normally, network kill, tier-drop
disclosure) aren't wired into one end-to-end demo yet — that would mean composing `AgentLoop` +
`TierResolver` + this script into a single script, still outstanding. Week 3 (`blackout_chaos`, §3)
is fully unstarted: injection points, scenario spec, five detectors, mock backend with effect
ledger, and the naive/framework baseline comparison.
