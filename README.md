<div align="center">

# Blackout
### Authority Runtime for Agents That Lose Connectivity

<p>
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776ab?style=for-the-badge&logo=python&logoColor=white" />
  <img src="https://img.shields.io/badge/pytest-103_tests-0A9EDC?style=for-the-badge&logo=pytest&logoColor=white" />
  <img src="https://img.shields.io/badge/SQLite-WAL-003B57?style=for-the-badge&logo=sqlite&logoColor=white" />
  <img src="https://img.shields.io/badge/Docker-Toxiproxy-2496ED?style=for-the-badge&logo=docker&logoColor=white" />
  <img src="https://img.shields.io/badge/OpenAI-Tier_1-412991?style=for-the-badge&logo=openai&logoColor=white" />
  <img src="https://img.shields.io/badge/Ollama-Tier_2-1a1a1a?style=for-the-badge" />
  <img src="https://img.shields.io/badge/Tamper_Evidence-HMAC--SHA256-brightgreen?style=for-the-badge" />
</p>

**Blackout** is an authority runtime for agents that lose connectivity, plus a chaos-testing harness that proves it under real network failure. Permission to execute a tool is a function of the agent's *current* capability tier, not a static grant — when the model backing the agent gets dumber, its authority shrinks with it. An action that outruns the current tier's authority becomes a durable, tamper-evident intent, never a dropped call and never a fabricated success.

> A tool's permission is tier-bound, not framework-bound. An offline write is either executed, durably deferred, or refused — never silently lost, never silently retried, never silently promised.

[Architecture](#system-architecture) · [Authority Pipeline](#the-authority-pipeline) · [Features](#features) · [Verified Findings](#verified-findings) · [Quick Start](#quick-start)

</div>

## What Makes This Different

Most agent frameworks treat "the model went offline" as an infrastructure problem to retry around. That's the wrong layer to solve it at.

| Typical Agent Framework | Blackout |
|---|---|
| Tool permissions are fixed regardless of which model is driving | Permission is a function of the live capability tier — a demoted model loses write access structurally, not by convention |
| An out-of-tier write is dropped, blindly retried, or silently fabricated | An out-of-tier write becomes a durable, HMAC-signed intent — never dropped, never silently retried |
| Replay after reconnect re-runs the original call against however the world looks now | Replay re-evaluates the exact preconditions that justified the deferral; stale evidence refuses instead of replaying |
| "The model is unavailable" is an unhandled exception | Tier 3 (no model, deterministic rules) and tier 0 (journal unavailable) are first-class states with defined behavior |
| Fault testing mocks the function boundary | Fault injection happens at the transport layer (Toxiproxy) — it's the actual HTTP client's retry logic under test, not your own code |
| A tool outside the model's authorized set is filtered from the output after the fact | At tier 2, the authorized set is compiled directly into the constrained-decoding grammar — an unauthorized tool is structurally absent, not discouraged |
| Chaos tests run once, against your own runtime, and call it proof | The same scenario suite is built to run unmodified against a naive retry-loop agent and a framework-default agent — a green matrix only means something in comparison |

## System Architecture

```
╔════════════════════════════════════════════════════════════════════════╗
║                    Blackout: Authority Decision Flow                   ║
╠════════════════════════════════════════════════════════════════════════╣
║                                                                          ║
║   Tool call proposed                                                    ║
║          │                                                              ║
║          ▼                                                              ║
║   ┌───────────────────┐                                                 ║
║   │    Tier Resolver    │   connectivity / journal / model probes,     ║
║   │    (hysteresis)      │   asymmetric: N successes to promote,       ║
║   └──────────┬──────────┘   one failure to demote                      ║
║              │                                                          ║
║      ┌───────┼────────────┬─────────────┐                              ║
║      ▼       ▼            ▼             ▼                              ║
║   Tier 1   Tier 2       Tier 3       Tier 0                            ║
║   cloud    local        fixed        JOURNAL_DOWN                      ║
║   full     reads +      rules,       reads only,                       ║
║   auth.    rev. writes  no model     every write refused               ║
║      │       │            │             │  checked by identity,        ║
║      └───┬───┴────────────┘             │  never numeric comparison    ║
║          ▼                              │                              ║
║   ┌────────────────────────┐            │                              ║
║   │      Policy Engine       │◄──────────┘                              ║
║   │  evaluate(tool, tier,    │  pure, synchronous — no I/O, no lock,   ║
║   │   args, preconditions)   │  never touches the journal itself        ║
║   └────────────┬────────────┘                                          ║
║                │                                                        ║
║      ┌─────────┼────────────┐                                          ║
║      ▼         ▼            ▼                                          ║
║   EXECUTE    REFUSE       DEFER                                        ║
║   tier is    precondition offline_policy=defer, preconditions          ║
║   capable    unsatisfied  captured + snapshotted at this instant       ║
║   enough     or already        │                                       ║
║              stale             ▼                                       ║
║                        ┌──────────────────┐                            ║
║                        │  Intent Journal    │  append-only, WAL SQLite,║
║                        │  (HMAC-signed)     │  every row HMAC-signed,  ║
║                        └─────────┬─────────┘  monotonic-only TTL       ║
║                                  │                                      ║
║                          reconnect / probe                             ║
║                                  ▼                                      ║
║                        ┌──────────────────┐                            ║
║                        │    Reconciler      │  expire → collapse dupes ║
║                        │                    │  → topo-sort → classify  ║
║                        └─────────┬─────────┘  → detect conflicts → sort║
║                                  ▼                                      ║
║              ready · ready_with_drift · rejected · collapsed           ║
║                                  │                                      ║
║                                  ▼                                      ║
║                        ┌──────────────────┐                            ║
║                        │  Approval Surface  │  precondition diffs,     ║
║                        │       (CLI)        │  IRREVERSIBLE warnings   ║
║                        └─────────┬─────────┘                           ║
║                                  ▼                                      ║
║                    approve → executes the deferred call,               ║
║                    marks REPLAYED only once it actually succeeds       ║
╚════════════════════════════════════════════════════════════════════════╝
```

```
╔════════════════════════════════════════════════════════════════════════╗
║                    Blackout: Chaos Harness Data Flow                   ║
╠════════════════════════════════════════════════════════════════════════╣
║                                                                          ║
║   YAML scenario                                                         ║
║   task · warmup_task · seed_inventory · inject.at · assert[]            ║
║              │                                                          ║
║              ▼                                                          ║
║   ┌─────────────────────────────────────────────────────────────┐     ║
║   │                  runner.py :: run_scenario                    │     ║
║   │  seed inventory → warmup task (primes read cache) → apply     │     ║
║   │  fault on the named proxy → run the faulted task → snapshot   │     ║
║   │  pending intents → reconcile → approve ready intents →        │     ║
║   │  evaluate every named detector                                │     ║
║   └──────┬──────────────────────┬───────────────────┬─────────────┘     ║
║          ▼                      ▼                    ▼                  ║
║   ┌──────────────┐     ┌──────────────────┐   ┌───────────────────┐   ║
║   │  Toxiproxy     │     │  MockBackendServer │   │  ChaosAgent         │   ║
║   │  (Docker)      │     │  threaded stdlib    │   │  Protocol            │   ║
║   │                │     │  HTTP server,        │   │                     │   ║
║   │  chaos_model_  │     │  undeduplicated      │   │  CoreAgentAdapter   │   ║
║   │   proxy :20000 │     │  effect ledger        │   │  wraps a real       │   ║
║   │  chaos_tool_   │     │  (ground truth for    │   │  AgentLoop +        │   ║
║   │   proxy :20001 │     │  duplicate-effect     │   │  IntentJournal +    │   ║
║   │                │     │  detection)            │   │  TierResolver       │   ║
║   │  7 network      │     └──────────┬───────────┘   └──────────┬──────────┘   ║
║   │  toxics + 1      │                │                          │               ║
║   │  tier-0 disk_    │                └───────────┬──────────────┘               ║
║   │  exhausted        │                            ▼                             ║
║   │  (bypasses         │                ┌────────────────────────┐               ║
║   │   Toxiproxy)        │                │      RunObservation      │             ║
║   └────────────────────┘                │  tool_calls, disclosures, │             ║
║                                          │  tier_transitions,        │             ║
║                                          │  pending/resolved ids,    │             ║
║                                          │  effect ledger             │             ║
║                                          └────────────┬───────────┘               ║
║                                                       ▼                            ║
║                                        ┌──────────────────────────┐               ║
║                                        │        5 Detectors          │             ║
║                                        │  fabrication · duplicate    │             ║
║                                        │  effect · silent            │             ║
║                                        │  degradation · lost work ·  │             ║
║                                        │  authority violation        │             ║
║                                        └────────────┬───────────┘               ║
║                                                     ▼                            ║
║                                        ┌──────────────────────────┐             ║
║                                        │  report.py :: render_matrix │           ║
║                                        │  scenario × detector table,  │           ║
║                                        │  markdown                    │           ║
║                                        └──────────────────────────┘             ║
╚════════════════════════════════════════════════════════════════════════╝
```

## The Authority Pipeline

Every tool call passes through the same four gates, whether it lands, waits, or dies.

### Gate 1: Tier Resolution

`TierResolver` computes the live tier from connectivity, journal-availability, and model-reachability probes — never from "is the socket open." Promotion requires `promote_after` consecutive good probes; demotion happens on a single failure. Optimistic promotion on a flaky link is worse than staying degraded: it starts a cloud-tier action the agent can't finish. Every transition is recorded and disclosed, never silent.

### Gate 2: Policy Evaluation

`PolicyEngine.evaluate(tool, tier, args, preconditions)` is pure and synchronous — no I/O, no locking, never touches the journal — so it can be tested and audited without a running system. Tier 0 (journal unavailable) short-circuits everything first: reads execute, every write refuses, checked by identity and never by numeric comparison against the other tiers. Missing or unsatisfied preconditions refuse regardless of tier. Otherwise, tier capability decides `EXECUTE`; failing that, the tool's own `offline_policy` decides `EXECUTE` (reads only), `REFUSE`, or `DEFER` — and `DEFER` itself refuses rather than queues if the precondition evidence was already stale the moment it was captured.

### Gate 3: Durable Deferral

A deferred intent is signed with HMAC over its content and appended to a WAL-mode SQLite journal. Expiry is computed from a monotonic clock, never wall time — an offline device's clock can drift or jump across a long partition in exactly the way that would let a stale intent slip through as fresh. Verification is lazy: it runs on every read, and on startup, and before any approval batch is built. A tampered row is quarantined to a `CORRUPT` status rather than silently trusted or silently dropped — it stays visible in the approval inbox as unreviewable.

### Gate 4: Reconciliation and Replay

On reconnect, the reconciler walks the pending queue in creation order: expire anything past its TTL, collapse duplicates by idempotency key, topologically sort by dependency so a same-run cascade of rejections is caught, re-evaluate every precondition against a fresh read, detect intra-batch resource conflicts, and sort the survivors so the shakiest justification is reviewed first. `ready` and `ready_with_drift` intents wait for a human via the approval CLI, which shows precondition diffs and flags irreversible actions before anything executes. Approval executes the original call and only then marks the intent replayed — never the reverse.

## Features

### Tiered Model Router
Three backends behind one interface: native OpenAI tool-calling at tier 1, JSON-Schema-constrained Ollama generation at tier 2, and a deterministic rules engine at tier 3 that also stands in for the other two in tests. `ModelRouter` hands each backend exactly the tools available at that tier — the offered set, not the full registry — so an unauthorized tool is absent from the schema itself, not filtered from the result.

### Read Cache and Precondition Registry
An in-memory cache backs offline reads with explicit staleness metadata. A named precondition maps a tool's own arguments to a cache key and a predicate; the same evaluation path serves both the agent loop's `DEFER` decision and the reconciler's replay-time re-check, so "was this ever fresh" and "is it still fresh" are answered by identical logic.

### Loop Checkpointing
A durable, HMAC-signed snapshot of a task's in-flight state, stored in the same journal file as the intent log. A crash mid-partition produces `orphaned` intents — visible and flagged as lacking context — instead of silently replaying or silently losing them.

### Reconciler and Approval Surface
The full seven-step reconnect algorithm (expire, collapse, topo-sort, classify, conflict-detect, sort) feeding a CLI review surface with precondition diffs, an `IRREVERSIBLE` warning on non-reversible actions, and an approve action that executes the deferred call rather than just flipping a status.

### Mock Backend with a Ground-Truth Ledger
A threaded stdlib HTTP server standing in for a real service. `POST /restock` always appends to its effect ledger, including exact duplicates — deliberately undeduplicated, because the duplicate-effect detector needs ground truth to diff the agent's own idempotency logic against, not an inferred one.

### Eight Fault Injectors
Seven Toxiproxy toxics modeling the agent loop as a state machine and injecting at each edge — `pre_plan`, `mid_plan`, `post_request_pre_response`, `partial_response`, `slow_success`, `flapping`, `recovery_storm` — plus a separate tier-0 `disk_exhausted` primitive that Toxiproxy structurally cannot reach, simulated directly against the journal's own write path.

### Five Behavioral Detectors
Mechanically checkable, no LLM judge required: fabrication, duplicate effect, silent degradation, lost work, and authority violation. Each is a pure function over a `RunObservation` — no I/O, no live infrastructure, testable against hand-built fixtures.

### YAML Scenario Runner and Report
A scenario spec (task, warmup task, seed inventory, injection point, assertions) drives `run_scenario`: seed, warm up the read cache, inject the fault, run the task, reconcile, approve, detect. `render_matrix` turns the result into a scenario × detector markdown table.

### Three-Way Comparison
The same scenario suite, the same tools, the same mock backend, and the same task run unmodified against Blackout's tiered runtime, a naive function-calling agent with a retry decorator and no tier awareness, and a framework-default agent — because a green matrix against your own runtime is unfalsifiable, and a comparison that only reports wins is a rigged comparison.

## Verified Findings

| Finding | Evidence |
|---|---|
| The tier-2 authority boundary is structural, not a runtime filter | Verified against Ollama 0.32.13 + `qwen2.5:1.5b`: a tool excluded from the tier's offered set cannot be named by the constrained grammar. Asked directly to page an on-call engineer with `page_oncall` excluded, the model was constrained into a different tool and could not emit the excluded call — it did try to smuggle the intent into a string argument, but the tool call itself couldn't carry it |
| A real transport-layer failure breaks things a mocked socket wouldn't | `write_ack_lost` drives an EXECUTE-tier call through a genuine Toxiproxy-induced ack loss against a real `CoreAgentAdapter` — the agent records exactly one crash, and the mock backend's ledger holds exactly one effect the agent has no record of ever receiving |
| A green detector matrix states its own blind spots rather than hiding them | Because the agent loop's EXECUTE path has no exception handling around the tool call itself, a crash under fault injection produces no call record and no journaled intent — so fabrication, duplicate-effect, and lost-work detectors structurally cannot see it. That gap is measured and reported next to the matrix, not papered over by it |

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11+ |
| Durable journal | SQLite, WAL mode, HMAC-SHA256 signed rows |
| Cloud model tier | OpenAI native tool-calling |
| Local model tier | Ollama, JSON-Schema-constrained generation |
| Rules tier | Deterministic substring-match, no model |
| Policy schema | stdlib `dataclasses` (`frozen=True, slots=True`) |
| Fault injection | Toxiproxy (Docker), transport-layer, not mocked sockets |
| Chaos mock backend | Threaded stdlib `http.server` / `socketserver` |
| Scenario spec | YAML (`pyyaml`) |
| Tests | pytest |
| Trace buffer | Offline-buffered spans, flushed on reconnect |

## Project Structure

```
Blackout/
│
├── blackout_core/
│   ├── policy.py                # ToolRegistry, PolicyEngine, TierResolver, Tier
│   ├── journal.py                # IntentJournal, Intent — WAL SQLite, HMAC, ULID ids
│   ├── router.py                 # ModelRouter, RulesBackend — dispatch by tier
│   ├── schema.py                 # per-tool arg schema from type annotations
│   ├── loop.py                    # AgentLoop.step() — resolve, propose, evaluate, act
│   ├── read_cache.py              # ReadCache, PreconditionRegistry
│   ├── checkpoint.py              # CheckpointStore, Checkpoint — same file as the journal
│   ├── reconciler.py              # reconcile() — the seven-step reconnect algorithm
│   ├── cli.py                     # build_inbox, format_inbox, approve, reject, run_interactive
│   └── backends/
│       ├── openai_backend.py      # tier-1: native tool-calling
│       └── ollama_backend.py      # tier-2: JSON-Schema-constrained generation
│
├── blackout_chaos/
│   ├── mock_backend.py            # MockBackendServer — threaded HTTP, undeduplicated ledger
│   ├── toxiproxy_client.py        # thin requests wrapper over Toxiproxy's admin API
│   ├── agent.py                   # ChaosAgent protocol, CoreAgentAdapter
│   ├── injection.py               # 7 Toxiproxy toxics + disk_exhausted
│   ├── scenario.py                # Scenario/InjectSpec dataclasses, YAML loader
│   ├── detectors.py               # the 5 pure behavioral detectors
│   ├── runner.py                  # run_scenario — wires everything together
│   ├── report.py                  # render_matrix — scenario × detector markdown table
│   └── scenarios/
│       └── write_ack_lost.yaml    # the post_request_pre_response milestone scenario
│
├── scripts/
│   ├── smoke.py                   # manual end-to-end policy engine + journal walkthrough
│   └── approval_inbox.py          # live reconcile → approval-batch demo, injectable clock
│
├── tests/                         # pytest suite — pure unit tests plus self-skipping
│                                   #   live tests (Ollama, Toxiproxy) that never block CI
│
├── docs/
│   └── blackout-design.md         # design doc — spec of record for every field, every choice
│
├── docker-compose.yml             # Toxiproxy: admin 8474, model proxy 20000, tool proxy 20001
├── pyproject.toml                 # dev / cloud / chaos extras
└── STATUS.md                      # implementation status, component by component
```

## Quick Start

### 1. Install

```bash
git clone https://github.com/Sahil-Khalsa/Blackout.git
cd Blackout
pip install -e ".[dev,cloud,chaos]"
```

### 2. Configure a cloud backend (optional)

```bash
cp .env.example .env
# set OPENAI_API_KEY — never commit .env
```

### 3. Run the core smoke test

```bash
python scripts/smoke.py
```

### 4. Pull a local model for tier 2 (optional)

```bash
ollama pull qwen2.5:1.5b
```

### 5. Start Toxiproxy for the chaos harness (optional)

```bash
docker compose up -d
```

### 6. Run the tests

```bash
pytest
# tier-2 and chaos/Toxiproxy tests self-skip if Ollama/Docker aren't reachable —
# they never block the rest of the suite
```

### 7. Walk the approval inbox live

```bash
python scripts/approval_inbox.py
```

## Key Commands

```bash
# Core smoke test — policy engine + journal, no infra required
python scripts/smoke.py

# Live approval-inbox walkthrough (one ready, one drifted, one auto-rejected as stale)
python scripts/approval_inbox.py

# Full suite
pytest

# Skip the live Ollama integration test explicitly
pytest -k "not ollama_integration"

# Single test
pytest tests/test_x.py::test_y

# Start Toxiproxy for the chaos harness
docker compose up -d

# Run one chaos scenario end to end (requires Toxiproxy running)
pytest tests/test_chaos_integration.py -v
```

## Key Engineering Decisions

**1. Tier 0 is a distinct failure axis, never compared numerically.**
`JOURNAL_DOWN` sits below every other tier in authority but isn't "more capable" than them. Every check tests it by identity (`is Tier.JOURNAL_DOWN`) first, never `<=` — because `0 <= min_tier` is true for every tool, and a numeric comparison would silently authorize a write with no durable place left to record it.

**2. Idempotency keys are pure functions of arguments, never random UUIDs.**
Two independently-formed intents for the same restock collapse into one on replay. A UUID would make every retry a new intent; a deterministic key makes replay safe under an agent's own retry loop.

**3. Preconditions are captured at defer time and re-evaluated at replay time.**
An approval formed against a world state that no longer exists is a liability, not a favor. `max_precondition_staleness_s` is enforced again against the *fresh* reading at reconcile time, not only at capture time.

**4. Every elapsed-time branch uses a monotonic clock, never wall time.**
TTL expiry, precondition staleness, and cache age all branch on elapsed time; a wall clock can drift or jump across an offline session in exactly the way that would let a stale intent slip through as fresh.

**5. Tier promotion needs consecutive good probes; demotion needs exactly one bad one.**
Optimistic promotion on a flaky link starts an action the agent can't finish, which is strictly worse than staying at the lower tier. Demotion is cheap; promotion is expensive — the asymmetry encodes that directly.

**6. The tier-2 authority boundary lives in the schema, not a post-hoc filter.**
The local model's grammar is compiled from the tier's offered set, not the full registry. An unauthorized tool is structurally absent from what the model can even emit, not filtered from its output afterward.

**7. Every journal row is HMAC-signed; a tampered row is quarantined, not trusted or dropped.**
Verification is lazy — on read, on startup, and before any approval batch is built. A single corrupt row moves to a `CORRUPT` status and stays visible in the approval inbox as unreviewable, rather than silently vanishing or poisoning the rest of the read.

**8. Fault injection happens at the transport layer, not the function boundary.**
Toxiproxy sits in front of a real threaded HTTP server, so what's under test is the HTTP client's own retry and timeout behavior against a genuine dropped connection — not a mock that only exercises your own code.

**9. The mock backend never deduplicates.**
`POST /restock` appends to the ledger even for byte-identical duplicate requests. The duplicate-effect detector needs ground truth to diff the agent's own idempotency logic against; a deduplicated ledger would make that detector unfalsifiable.

**10. A green matrix against your own runtime proves nothing on its own.**
The same scenario suite is designed to run, unmodified, against a naive retry-loop agent and a framework-default agent with identical tools, mock backend, and task. Reporting the scenarios where the baseline is fine too is part of the same discipline — a comparison that only shows wins is a rigged comparison.

**11. `disk_exhausted` is a separate primitive from the seven network faults.**
Toxiproxy can't reach a full local disk, so a full-disk write failure is simulated directly against the journal's own SQLite connection — restoring both the connection and the journal's internal availability flag on exit, so the fault doesn't outlive the window that triggered it.

## Domain Reference

**Tiers**

| Tier | Name | Authority |
|---|---|---|
| 1 | Cloud | Full authority |
| 2 | Local | Reads plus reversible writes |
| 3 | Rules | No model in the loop, whitelisted paths only |
| 0 | Journal unavailable | Reads only — every write refused |

**Offline policy** — what happens when the current tier can't execute a tool directly:

| Policy | Meaning |
|---|---|
| `EXECUTE` | Reads only — serve from cache regardless of tier |
| `DEFER` | A durable intent is queued for reconciliation and human approval |
| `REFUSE` | Worthless or harmful late — nothing is queued |

**Fault injection points**

| Point | Fault |
|---|---|
| `pre_plan` | Network dies before the model call |
| `mid_plan` | Model response stream cut mid-token |
| `post_request_pre_response` | Tool request sent, response never arrives |
| `partial_response` | Truncated or malformed body returned |
| `slow_success` | Response arrives after the client timed out |
| `flapping` | Up/down/up within a single loop iteration |
| `recovery_storm` | Connectivity restored, all queued work fires simultaneously |
| `disk_exhausted` | Tier-0 — the journal's own write path fails |

**Detectors**

| Detector | Checks |
|---|---|
| Fabrication | An executed call claims a result the ledger never received |
| Duplicate effect | Two ledger rows share one idempotency key |
| Silent degradation | A tier transition with no matching user-visible disclosure |
| Lost work | An intent pending at partition time is neither replayed nor rejected |
| Authority violation | A tool executed at a tier below its declared `min_tier` |

## Author

Built by **Sahilsingh Khalsa**

<sub>Python · SQLite · OpenAI · Ollama · Toxiproxy · pytest</sub>
