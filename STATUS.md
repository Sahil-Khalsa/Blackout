# Status

Last updated: 2026-08-19. Full rationale for every item lives in `docs/blackout-design.md`; this
file tracks only what exists versus what doesn't.

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
| Loop checkpoint | not started | journal has the *hooks* (`task_checkpoint_id`, `orphan_missing_checkpoints`) but nothing creates/stores a checkpoint yet |
| Reconciler | not started | §2.6 — journal exposes the primitives one needs (`pending`, `duplicates_of`, `by_status`, `resolve`), but the reconcile algorithm itself (expire → topo-sort `depends_on` → collapse duplicates → re-evaluate preconditions → classify ready/ready_with_drift/rejected → detect conflicts) isn't written |
| Approval surface | not started | CLI review of the batch with precondition diffs |
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

Verification: `scripts/smoke.py` (manual, policy engine + journal only) plus a real `tests/` suite
— 22 tests, `pytest` (~8s without Ollama reachable; the live Ollama integration test self-skips
when it isn't).

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

Rest of Week 2 (§5): loop checkpointing (§2.8) so a crash mid-partition produces `orphaned` intents
instead of silent loss, then the reconciler (§2.6, expire → topo-sort → collapse duplicates →
re-evaluate preconditions → classify → detect conflicts), then the CLI approval inbox with diffs.
The interactive "kill the network, watch the tier badge drop, restore, see the approval batch" demo
(§7) is still outstanding — current proof is automated tests, not a live walkthrough.
