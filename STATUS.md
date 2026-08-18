# Status

Last updated: 2026-08-18. Full rationale for every item lives in `docs/blackout-design.md`; this
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
| Minimal agent loop | done (Week 1 scope) | `loop.py::AgentLoop` |
| Read cache | not started | §2.7 — offline reads with staleness metadata |
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

### Agent loop detail

`loop.py::AgentLoop.step()` reads the current tier from its `TierResolver`, asks the `ModelRouter`
to propose a call, evaluates it through `PolicyEngine`, and executes/defers/refuses. Backend
failures feed back into tier resolution per §2.9's stated fallback: `BackendUnavailable` records a
failed probe; a `StructuralFailure` from the tier-2 backend marks the local model unavailable
(forces the resolver to tier 3) rather than retrying unconstrained.

**Scope decision (deliberate):** the loop does not fetch preconditions itself — that's the read
cache's job, not yet built. `DEFER` only works today for tools whose preconditions the caller
supplies directly to `step()`. The Week 1 milestone (refusing a tier-1-only tool after a
simulated network pull) doesn't need preconditions and is covered by
`tests/test_loop.py::test_agent_refuses_page_oncall_after_network_pull`. Demonstrating
`place_restock_order` actually deferring is Week 2 scope, once the read cache exists.

Verification: `scripts/smoke.py` (manual, policy engine + journal only) plus a real `tests/` suite
— 14 tests, `pytest` (~22s, dominated by live CPU inference in the Ollama integration test).

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

Rest of Week 2 (§5): a minimal read cache (§2.7) so `place_restock_order` can actually defer with
real preconditions, then loop checkpointing and the reconciler. The interactive "kill the network,
watch the tier badge drop, restore, see the approval batch" demo (§7) is still outstanding —
current proof is automated tests, not a live walkthrough.
