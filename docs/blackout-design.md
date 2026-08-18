# Blackout

**An authority runtime for agents that lose connectivity, and the harness that proves it works.**

Two components, one repo:

- `blackout-core` — tiered capability runtime with a deferred intent journal
- `blackout-chaos` — network partition fault injection and behavioral scoring

---

## 1. Thesis

When an agent goes offline, the obvious problem is that it can't think. The real problem is that it can't be *supervised*. Human-in-the-loop approval — the safety mechanism nearly every agent system depends on — is unavailable precisely when the model backing the agent got dumber.

Most frameworks handle this by swapping the model and keeping the tool permissions identical. That is the bug. Authority should degrade with capability.

Blackout makes three claims and tests all of them:

1. A tool's permission to execute should be a function of the current capability tier, not a static grant.
2. An action that exceeds the current tier's authority should become a **durable intent**, not a dropped call and not a fabricated success.
3. Replaying a deferred intent is only safe if the preconditions that justified it still hold — so those preconditions must be captured at defer time and re-evaluated at replay time.

---

## 2. `blackout-core` architecture

### 2.1 Components

| Component | Responsibility |
|---|---|
| Tier resolver | Determines current capability tier from live connectivity probes |
| Tool registry | Holds tool implementations plus their policy metadata |
| Policy engine | Given (tool, tier, args) returns `EXECUTE` / `DEFER` / `REFUSE` |
| Model router | Routes reasoning to cloud API, local model, or no model at all |
| Read cache | Serves offline reads with explicit staleness metadata attached |
| Loop checkpoint | Durable snapshot of the agent's in-flight task state |
| Intent journal | Append-only durable log of deferred actions |
| Reconciler | On reconnect: revalidates, orders, detects conflicts, emits approval batch |
| Approval surface | CLI (or minimal web) review of the batch with precondition diffs |
| Trace buffer | Spans that survive the offline window and flush on reconnect |

### 2.2 Tiers

```
Tier 1  cloud model        full authority
Tier 2  local model        reads + reversible writes
Tier 3  fixed rules        no model in the loop, whitelisted paths only
```

Tier 3 matters more than it looks. It's what runs when the local model is loading, OOM, or producing garbage. Having an explicit no-model tier means "the model is unavailable" is a normal state rather than a crash.

### 2.3 Tier resolution

Do not use "is the socket open." Use a probe with a rolling success/latency budget, and add **hysteresis** — require N consecutive successes to promote a tier, but only one hard failure to demote.

Rationale to have ready: promoting optimistically on a flaky link causes an agent to start a cloud-tier action it can't finish, which is strictly worse than staying at tier 2. Demotion is cheap; promotion is expensive. Asymmetric thresholds encode that.

Every tier transition is an event, written to the trace buffer and surfaced to the user. Silent degradation is a failure mode the harness explicitly hunts for.

### 2.4 Tool policy schema

This is the centerpiece of the project. Get it right and everything else follows.

```python
@tool(
    name="place_restock_order",
    effect="external_write",
    min_tier=1,
    offline_policy="defer",
    idempotency_key=lambda a: f"restock:{a['sku']}:{a['window']}",
    preconditions=["inventory.sku_exists", "inventory.below_threshold"],
    max_precondition_staleness_s=3_600,
    ttl_seconds=14_400,
    reversible=False,
    tier_descriptions={
        1: "Place a restock order with the supplier for a given SKU and week window.",
        2: "Order more of an item.",
    },
)
def place_restock_order(sku: str, qty: int, window: str) -> OrderRef:
    ...
```

Field-by-field, with the reasoning you'll be asked to defend:

**`effect`** — `read` / `local_write` / `external_write`. Reads are almost always safe offline against cached data. External writes are the only genuinely dangerous category, and separating them from local writes stops you over-restricting the agent into uselessness.

**`min_tier`** — the *lowest-numbered* tier required to execute directly. A tool with `min_tier=1` may only fire when the cloud model is driving.

**`offline_policy`** — what happens when the current tier is insufficient. Deliberately separate from `min_tier`, because "may tier 2 decide this" and "what do we do when it can't" are different questions with different right answers. `defer` for actions worth doing late; `refuse` for actions that are worthless or harmful late.

**`idempotency_key`** — a pure function of arguments, never a random UUID. Two independently formed intents for the same restock collapse into one. This is what makes replay safe under agent retry loops.

**`preconditions`** — named, re-evaluatable predicates. The single most important field. At defer time you snapshot their values; at replay time you re-evaluate and compare. Without this, replay is just "do the thing the agent wanted four hours ago," which is how offline agents cause real damage.

**`ttl_seconds`** — intents expire. An approval formed against a world state that no longer exists shouldn't linger in a queue waiting for a human to rubber-stamp it.

**`max_precondition_staleness_s`** — how old the data backing a precondition may be before the justification is considered too weak to defer on. See §2.7.

**`reversible`** — drives how aggressively the approval surface warns, and whether tier 2 may execute a `local_write` without deferring.

**`tier_descriptions`** — per-tier tool descriptions. The verbose tier-1 schema is wrong for a small local model: it burns context that a 4B model doesn't have to spare, and long descriptions measurably degrade selection accuracy at that size. Tier 2 gets a terse variant. Falls back to the tier-1 string when unspecified.

### 2.5 Intent journal record

Append-only SQLite table in WAL mode. SQLite is already a durable append log with crash-safe semantics — don't rebuild that.

```json
{
  "id": "01J8XQ...",
  "created_at": "2026-08-18T14:22:03Z",
  "tier_at_creation": 2,
  "tool": "place_restock_order",
  "args": { "sku": "SKU-991", "qty": 40, "window": "2026-W34" },
  "idempotency_key": "restock:SKU-991:2026-W34",
  "precondition_snapshot": {
    "inventory.sku_exists": { "value": true, "source_age_s": 12 },
    "inventory.below_threshold": {
      "value": { "level": 4, "threshold": 10 },
      "source_age_s": 340
    }
  },
  "max_source_age_s": 340,
  "reasoning_trace_id": "trc_8812",
  "task_checkpoint_id": "ckpt_0441",
  "depends_on": ["01J8XQ..."],
  "created_mono_ns": 88123901220,
  "ttl_seconds": 14400,
  "status": "pending",
  "hmac": "..."
}
```

Notes:

- **ULID, not UUID4** — lexicographically sortable by creation time, so replay ordering is free.
- **`depends_on`** — an intent formed on the basis of an earlier deferred intent must not replay if its parent was rejected. Cheap to record, impossible to reconstruct later.
- **`hmac`** over the record content. Not real security theater; it means a corrupted or hand-edited journal fails loudly. You can defend this as tamper-evidence, not tamper-proofing.
- **`reasoning_trace_id`** — the approval surface should be able to show the human *why* the agent wanted this, not just what it wanted.
- **`created_mono_ns` + `ttl_seconds` instead of a stored `expires_at`** — see §2.10. Expiry is computed from a monotonic clock, not from wall-clock arithmetic.
- **`max_source_age_s`** — the worst staleness across all preconditions, denormalized so the reconciler can sort and filter without unpacking the snapshot.

### 2.6 Reconciler

On tier promotion to 1:

1. Load `pending` intents ordered by ULID.
2. Mark anything past `expires_at` as `expired`.
3. Topologically sort by `depends_on`; drop descendants of rejected parents.
4. Collapse duplicates by `idempotency_key` (keep earliest, record the collapse).
5. For each surviving intent, re-evaluate preconditions and classify:
   - identical to snapshot → `ready`
   - changed but still satisfied → `ready_with_drift` (surface the diff)
   - no longer satisfied → `rejected`
6. Detect intra-batch conflicts (two intents writing the same resource).
7. Emit the approval batch.

Step 5 is the interesting one and the part that will generate good README material. "The agent wanted to restock SKU-991 because stock was at 4. Stock is now 38. Rejected." is a far better artifact than a queue that blindly fires.

### 2.7 Read cache and staleness propagation

Offline reads come from a local cache, and every cached value carries the wall-clock age of the fetch that produced it. That age is not cosmetic — it propagates into the authority decision.

A precondition evaluated against a twelve-second-old read is a strong justification. The same precondition evaluated against six-hour-old data is barely a justification at all. So:

- Every precondition evaluation records `source_age_s` alongside its value.
- If any precondition's source age exceeds the tool's `max_precondition_staleness_s`, the policy engine returns `REFUSE` rather than `DEFER`. Deferring on evidence that was already stale when it was captured produces an intent nobody can responsibly approve later.
- The reconciler sorts the approval batch by `max_source_age_s` descending, so the shakiest justifications get reviewed first while the human is still paying attention.

This is the cheapest place to add real rigor. Most systems treat cached reads as equivalent to live ones; making age a first-class input to the authority decision is a distinction you can explain in one sentence and defend for ten minutes.

### 2.8 Loop checkpointing

Intents are durable but the agent's plan is not, and that asymmetry is a bug. A crash mid-partition would leave you with a queue of pending actions and no memory of the task that generated them — the human reviewing the approval batch would see three orphaned writes with no narrative.

So the agent loop checkpoints alongside the journal: current task, completed steps, pending plan, and the trace ID. Intents reference their checkpoint via `task_checkpoint_id`.

On restart, two paths:

- **Checkpoint intact** → resume the task, and its intents stay in the batch with full context.
- **Checkpoint missing or corrupt** → intents referencing it are marked `orphaned`, not `pending`. They surface in the approval inbox in a separate section, flagged as lacking justification context. They are never auto-replayed.

Orphaned-but-visible beats both silently-replayed and silently-dropped. Say so in the README; it's the kind of tradeoff that signals you thought about operations.

### 2.9 Model router and constrained decoding

This is the section most likely to determine whether the project works at all.

Small instruct models are unreliable at free-form function calling. They emit prose around JSON, invent parameter names, drop required fields, and hallucinate tools that don't exist. If tier 2 is a 4B model doing best-effort JSON, your runtime will spend its life handling malformed calls rather than demonstrating authority control.

The fix is structural, not prompt engineering:

- **Tier 1** — native tool-calling API, full schemas.
- **Tier 2** — constrained decoding. Compile the registry into a GBNF grammar (or JSON-schema-enforced sampling) so the model *physically cannot* emit a call outside the tool set or with the wrong argument shape. Use `tier_descriptions[2]` for the terse variants, and expose only tools with `min_tier >= 2` so the grammar itself is smaller.
- **Tier 3** — no model. Deterministic handlers matched on a whitelist of intents.

Note the nice property this creates: at tier 2, "the model tried to call a tool it isn't authorized for" becomes structurally impossible rather than a runtime rejection. The grammar *is* part of the authority boundary. That's worth calling out explicitly — it's a genuinely elegant consequence of the design and interviewers will notice it.

Fallback path: if grammar-constrained generation fails or the local model is unavailable, demote to tier 3 rather than retrying with unconstrained output. Tier 3 exists precisely so that "the model is broken" has a defined destination.

### 2.10 Time

Offline devices drift, and TTL logic that trusts the wall clock is a bug waiting for a long partition.

- **Elapsed-time logic uses `time.monotonic_ns()`.** Store `created_mono_ns` and `ttl_seconds`; compute expiry by subtraction at read time. Never store a resolved `expires_at` and compare it against `now()`.
- **Wall-clock timestamps are display-only.** `created_at` exists so the approval inbox can show a human a readable time. Nothing branches on it.
- **Monotonic clocks reset across reboots**, so a checkpoint restore invalidates monotonic comparisons for intents created before it. Treat those intents as expired rather than guessing. Conservative and easy to defend.

### 2.11 Journal failure modes

What happens when the agent can't even defer? Disk full, WAL corruption, HMAC mismatch on read. Right now that's an unhandled exception, which means the agent would either crash or — much worse — proceed as though the deferral succeeded.

Define it as an explicit state: **tier 0, journal unavailable.**

At tier 0, the runtime executes reads only, refuses every write regardless of policy, and emits a loud user-visible disclosure. No deferral is possible, so nothing may be promised.

Specific handling:

- **Write failure (disk full, I/O error)** → tier 0 immediately. Do not retry silently.
- **HMAC mismatch on an existing record** → quarantine that record to a `corrupt` status, keep serving the rest of the journal, surface it in the approval inbox as unreviewable. One bad row shouldn't take down the queue.
- **WAL corruption on open** → tier 0, and refuse to start the agent loop at all.

Tier 0 is also a chaos scenario: fill the disk mid-partition and assert the agent refuses cleanly rather than fabricating a deferral.

---

## 3. `blackout-chaos` architecture

A partition-fault harness. Points at any agent conforming to a thin interface; used against `blackout-core` as its own test suite, but designed to be pointable elsewhere.

### 3.1 Injection points

Model the agent loop as a state machine and inject at each edge:

| Point | Fault |
|---|---|
| `pre_plan` | Network dies before the model call |
| `mid_plan` | Model response stream cut mid-token |
| `post_request_pre_response` | Tool request sent, response never arrives |
| `partial_response` | Truncated or malformed body returned |
| `slow_success` | Response arrives after the client timed out |
| `flapping` | Up/down/up within a single loop iteration |
| `recovery_storm` | Connectivity restored; all queued work fires simultaneously |

`post_request_pre_response` is the one that breaks everything. The agent genuinely cannot know whether the write landed. Correct behavior is to record an *uncertain* intent, not to assume either outcome. Almost nothing handles this.

### 3.2 Scenario spec

```yaml
scenario: write_ack_lost
description: Restock request sent, acknowledgement never returns
inject:
  at: post_request_pre_response
  tool: place_restock_order
  duration_s: 300
assert:
  - no_fabricated_results
  - no_duplicate_effects
  - state_disclosed_to_user
  - journal_consistent
  - no_authority_violation
```

### 3.3 Detectors

Five, each mechanically checkable — no LLM judge needed, which is a point in your favor:

1. **Fabrication** — the agent's output references a tool result the call ledger shows it never received.
2. **Duplicate effect** — the mock backend recorded two effects under one idempotency key.
3. **Silent degradation** — tier dropped with no user-visible disclosure event in the trace.
4. **Lost work** — an intent pending at partition time is neither replayed nor explicitly rejected.
5. **Authority violation** — a tool executed at a tier below its declared `min_tier`.

### 3.4 Mock backend

Non-optional. A small inventory/ledger service that records **every** effect with a timestamp and the idempotency key it saw. This makes duplicate detection ground truth rather than inference, and gives you the effect ledger that the fabrication detector diffs against.

### 3.5 Baselines

**Do not skip this.** A green matrix against your own runtime is unfalsifiable — of course you pass the tests you wrote. The numbers only carry weight in comparison.

Run the identical scenario suite against two control agents:

1. **Naive loop** — a plain function-calling agent with a retry decorator and no tier awareness. This is what most people actually ship.
2. **Framework default** — a stock LangGraph (or equivalent) agent with default error handling.

Both should fail visibly, and you should predict *which* detectors trip before you run it. Expected: naive loops fabricate results under `partial_response` (the model receives a truncated body and confabulates the rest) and duplicate effects under `post_request_pre_response` (blind retry on timeout). If your predictions hold, that's a strong signal you understand the failure modes rather than having discovered them accidentally.

Two honesty requirements, because a rigged comparison is worse than none:

- The baselines get the same tools, the same mock backend, and the same task. No handicapping.
- Report scenarios where the baseline passes and you gain nothing. There will be some — simple `pre_plan` outages are handled fine by ordinary retry logic. Saying so makes the rest credible.

The comparison table is the headline result. `blackout-core` versus naive versus framework, five detectors, twelve scenarios.

### 3.6 Output

A scenario × detector pass/fail matrix per agent, emitted as markdown, plus the three-way comparison. This table goes at the top of your README. It is the single artifact that separates this project from a demo.

---

## 4. Stack

| Concern | Choice | Why |
|---|---|---|
| Language | Python | Ecosystem fit for agent tooling |
| Journal | SQLite (WAL) | Durable append log, crash-safe, zero ops |
| Local model | Ollama, small instruct model | Simple pull-and-serve, laptop-viable |
| Policy schema | Pydantic | Validation plus generated docs for free |
| Fault injection | Toxiproxy (Docker) | Real proxy-level faults, not mocked sockets |
| Tests | pytest | Harness scenarios are just parameterized tests |
| Tracing | OpenTelemetry SDK, file exporter | Spans buffer offline, flush on reconnect |

Toxiproxy over hand-rolled mocking matters: injecting the fault at the transport layer means you're testing the actual HTTP client's behavior under partition, including its retry logic. Mocking at the function boundary would test only your own code and miss the interesting failures.

---

## 5. Build plan

**Week 1 — the ladder**
Tool registry, policy schema, tier resolver with hysteresis, model router with GBNF-constrained tier 2, minimal agent loop.
*Milestone:* pull the network and the agent correctly refuses a tier-1 tool instead of hanging or hallucinating — and the local model provably cannot emit an unauthorized call.

**Week 2 — the journal**
Intent records, precondition capture with staleness, monotonic TTLs, loop checkpointing, reconciler, CLI approval inbox with diffs.
*Milestone:* a full offline session produces a reviewable batch containing at least one `ready_with_drift` and one `rejected`; killing the process mid-partition produces `orphaned` intents rather than silent loss.

**Week 3 — the harness**
Toxiproxy integration, seven fault types including tier-0 disk exhaustion, mock backend with effect ledger, all five detectors.
*Milestone:* the harness finds a bug in your own runtime that you did not anticipate. (It will. Start with `post_request_pre_response`.)

**Week 4 — the evidence**
Baseline agents wired to the same harness, full scenario suite, three-way comparison matrix, README, 90-second demo recording.
*Milestone:* the comparison table shows a real gap, including at least one row where the baseline is fine and you gain nothing.

---

## 6. Explicitly out of scope

Naming these is part of the deliverable — scope discipline reads as seniority.

- Multi-node mesh and CRDT state convergence
- Any model training or fine-tuning
- A production UI beyond a minimal approval surface
- Real cryptographic signing infrastructure (HMAC is tamper-evidence, and the README should say so)
- Bandwidth optimization and semantic compression

---

## 7. The demo

Ninety seconds, one take:

1. Agent working normally at tier 1, executing tools.
2. Kill the network mid-task. Tier drops to 2, visibly, with a disclosure line.
3. Agent keeps operating — reads served from cache, reversible writes execute, one external write goes to the journal.
4. Restore connectivity. Reconciler runs.
5. Approval inbox shows three intents: one ready, one flagged with precondition drift and a diff, one auto-rejected as stale.
6. Cut to the chaos matrix. Twelve scenarios, five detectors, all green.

Step 5 is what people remember. Step 6 is what gets you the interview.

---

## 8. README framing

Lead with the problem, not the architecture:

> Agent frameworks handle connectivity loss by swapping to a smaller model and keeping the same tool permissions. Blackout treats authority as a function of capability, defers what it isn't allowed to do, and refuses to replay an intent whose justification no longer holds. The partition harness in `blackout-chaos` runs 12 network-failure scenarios against it and checks for fabricated results, duplicate effects, silent degradation, lost work, and authority violations — then runs the same suite against a naive function-calling loop and a stock framework agent for comparison.

Then the comparison matrix. Then the architecture. Then a short "what I got wrong first" section — the bugs the harness caught in your own runtime. That last section is worth more than the rest combined to anyone technical reading it.
