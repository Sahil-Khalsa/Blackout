# `blackout_chaos` harness — design spec

Status: proposed, pending review. Implements Week 3 of `docs/blackout-design.md` §5
(`docs/blackout-design.md` §3 is the spec of record for behavior; this document is the
field-by-field implementation plan for it, in the same spirit CLAUDE.md asks `blackout_core`
extensions to follow).

## 1. Scope

**In scope (this spec, Week 3):** the harness itself — mock backend, Toxiproxy orchestration, a
thin pluggable agent interface, all 7 network injection points from §3.1 plus the tier-0
disk-exhaustion fault from §2.11, the scenario spec + runner, all 5 detectors, and a markdown
output matrix — proven against `blackout_core` with at least the `write_ack_lost` scenario from
§3.2 running end to end.

**Out of scope (Week 4, separate spec later):** naive-loop and framework-default baseline agents,
the full 12-scenario suite, and the three-way comparison table (§3.5–3.6). The harness has a hard
sequential dependency on this spec, so it can't start first.

Also out of scope: a persisted OpenTelemetry trace buffer in `blackout_core` (STATUS.md still lists
this as not started). The harness's disclosure-tracking (§4.3 below) is deliberately decoupled from
that — it doesn't need to exist for the harness to work, and blocking on it would be an unforced
scope merge of two independent pieces.

## 2. Package layout

```
blackout_chaos/
    __init__.py         # public exports (stays import-light; live-infra pieces imported explicitly,
                         # same convention as blackout_core.backends)
    mock_backend.py      # MockBackendServer: threaded stdlib HTTP server, in-memory ledger
    toxiproxy_client.py  # ToxiproxyClient: thin requests-based wrapper over the admin API
    agent.py             # ChaosAgent protocol, ToolCallRecord, CoreAgentAdapter
    injection.py         # the 7 network injectors + the disk-exhaustion guard
    scenario.py          # Scenario dataclass, YAML loader
    detectors.py         # the 5 pure detector functions + RunObservation
    runner.py            # wires everything together: run one scenario, produce a ScenarioResult
    report.py            # markdown scenario x detector matrix
    scenarios/
        write_ack_lost.yaml
        ...
tests/
    test_chaos_mock_backend.py
    test_chaos_detectors.py       # pure, no infra -- hand-built RunObservation fixtures
    test_chaos_injection.py       # live, self-skips if Toxiproxy unreachable
    test_chaos_scenario.py        # YAML loader, pure
    test_chaos_integration.py     # live, self-skips if Toxiproxy unreachable
docker-compose.yml        # `docker compose up -d` starts Toxiproxy; documented in README
```

## 3. Mock backend (§3.4)

`MockBackendServer` — a `socketserver.ThreadingMixIn` + `http.server.HTTPServer` subclass (threaded,
not the default single-connection-at-a-time server: a fault-injection test can hold one connection
open under a `timeout` toxic, and the server must still answer the test's own `/ledger` polling
request on a different connection without deadlocking). Runs as a background thread inside the test
process — no subprocess, no Docker. Bound to port 0 (OS-assigned) so parallel test runs never
collide; the actual port is read back after bind and handed to `ToxiproxyClient.create_proxy` as the
upstream.

State: `_inventory: dict[str, int]`, `_ledger: list[EffectRecord]`.

Endpoints:
- `GET /inventory/<sku>` → `{"sku": sku, "level": N}` (seeded via `POST /inventory/<sku>` for
  scenario setup; defaults to 100 if never seeded).
- `POST /restock` body `{"sku","qty","window","idempotency_key"}` → **always appends** to the
  ledger, including exact duplicates. The backend does not deduplicate — it is deliberately dumb and
  honest, recording whatever it received, so the duplicate-effect detector has ground truth to diff
  the agent's own idempotency logic against rather than inferring it.
- `GET /ledger` → the full list of `EffectRecord{id, received_at, tool, idempotency_key, payload}`.
- `POST /reset` → clears the ledger and resets inventory to defaults, called between scenarios.

## 4. Agent interface

### 4.1 The `ChaosAgent` protocol

The harness drives and observes any agent through this minimal surface:

```python
class ChaosAgent(Protocol):
    def run_task(self, task: str) -> None: ...
    def tool_calls(self) -> list[ToolCallRecord]
    def disclosures(self) -> list[str]
    def pending_work_ids(self) -> list[str]
    def resolved_work_ids(self) -> set[str]
```

```python
@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    tool: str
    args: dict[str, Any]
    tier_at_call: int
    idempotency_key: str | None
    outcome: str          # "executed" | "deferred" | "refused"
    result: Any = None
```

`run_task` maps to exactly one `AgentLoop.step()` call for the `blackout_core` adapter — the loop is
still single-shot (STATUS.md), so a chaos "task" is one step, not a multi-turn session. Multi-step
scenarios aren't in scope until the loop itself grows that notion. If `step()` returns no proposal at
all (backend unavailable, or the router simply had nothing to call), `tool_calls()` records nothing
for that call — there was no attempted call to describe, so nothing goes in the ledger of what the
agent claims to have done.

### 4.2 `CoreAgentAdapter` (the `blackout_core` implementation)

Wraps an `AgentLoop` + its `IntentJournal` + `TierResolver`:

- `tool_calls()` — one `ToolCallRecord` per `step()` call, built from the returned `StepResult`
  (`tier_at_call=result.tier`, `outcome` from `result.decision.decision`).
- `pending_work_ids()` → `[i.id for i in journal.pending()]`.
- `resolved_work_ids()` → union of ids from `journal.by_status(s)` for every status in
  `{REPLAYED, REJECTED, EXPIRED, CONFLICTED, COLLAPSED, ORPHANED}`. **Deliberately excludes
  `CORRUPT`** — a corrupted record is neither replayed nor explicitly rejected, so it should trip
  the lost-work detector by the detector's own literal definition, not be quietly counted as
  resolved.

### 4.3 Disclosures, decoupled from the (unbuilt) trace buffer

`disclosures()` reuses `TierResolver.transitions` (already exists) and formats each transition as a
human-readable string. For `blackout_core`, every tier transition is disclosure-by-construction —
which means the silent-degradation detector can never fail against `blackout_core`'s own adapter as
currently written. That's expected, not a gap: the detector's value shows up once a naive baseline
(Week 4) is wired in, since a naive loop has no tier awareness and won't reliably surface transitions
at all. The silent-degradation detector's own unit tests (§8, §10) validate it against a
hand-constructed `RunObservation` with a transition that has no matching disclosure string — a
deliberately-broken fixture, not the real adapter, is what proves the detector actually catches a
suppressed disclosure.

## 5. Toxiproxy orchestration

`ToxiproxyClient` — thin `requests` wrapper over Toxiproxy's admin REST API (default
`localhost:8474`, no Toxiproxy-specific pip dependency):

- `create_proxy(name, listen, upstream)`, `delete_proxy(name)`
- `add_toxic(proxy, type, stream, **attributes)`, `remove_toxic(proxy, name)`
- `set_enabled(proxy, enabled: bool)` — full cut/restore, used by `pre_plan`, `flapping`, and
  recovery
- `reset()` — Toxiproxy's own reset endpoint, clears all toxics on all proxies; called between
  scenarios

Docker isn't launched by the test suite. Same convention as the existing Ollama live test: a
`docker_toxiproxy_available()` check tries `localhost:8474` and the live tests self-skip if it's
unreachable. `docker-compose.yml` at the repo root starts Toxiproxy (`docker compose up -d`),
documented in the README next to the existing Ollama setup note.

Two proxies are created per live test run: one in front of the model backend (Ollama, normally
`localhost:11434`) and one in front of the mock backend (§3). `pre_plan`/`mid_plan` faults target
the model proxy; `post_request_pre_response`/`partial_response`/`slow_success`/`flapping` target the
tool proxy.

## 6. Injection points (§3.1) → mechanism

| Point | Mechanism |
|---|---|
| `pre_plan` | `set_enabled(model_proxy, False)` before the model call |
| `mid_plan` | `limit_data` toxic on the model proxy's downstream stream |
| `post_request_pre_response` | `timeout` toxic on the tool proxy's downstream stream |
| `partial_response` | `limit_data` toxic on the tool proxy's downstream stream |
| `slow_success` | `latency` toxic on the tool proxy exceeding the client's configured timeout |
| `flapping` | `set_enabled` toggled off/on N times within one scenario step |
| `recovery_storm` | scenario-runner behavior, not a toxic: accumulate several deferrals under a `down` tool proxy, then `set_enabled(True)` + immediately reconcile, checking the ledger for duplicates under the burst |

**Tier-0 disk exhaustion (§2.11)** is not a network fault and Toxiproxy can't touch it — it's a
separate `injection.py` primitive: a context manager that wraps the journal's SQLite connection so
its next write raises `sqlite3.OperationalError("database or disk is full")`, then restores normal
behavior on exit. This resolves the spec ambiguity flagged during design discussion (§3.1's table
lists exactly 7 network faults; the Week 3 milestone text separately says "seven ... including
tier-0 disk exhaustion") as **8 total fault injectors**: the 7 network ones plus this one. Logged in
STATUS.md's discrepancies section, not silently picked.

## 7. Scenario spec + runner

YAML, matching §3.2's shape with one necessary addition — the doc's example doesn't include the
task text actually fed to the agent, which the runner needs:

```yaml
scenario: write_ack_lost
description: Restock request sent, acknowledgement never returns
task: "restock SKU-991, we're almost out"
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

`Scenario` (frozen dataclass) mirrors this shape exactly. `load_scenario(path)` /
`load_all_scenarios(dir)` use `yaml.safe_load` — new `chaos` extra in `pyproject.toml`:
`chaos = ["requests", "pyyaml"]`.

The `assert` strings are §3.2's own names, not the detector function names from §8 — they read as
positive assertions about the *outcome*, not as detector names, so the runner needs an explicit
mapping rather than a name match:

| `assert` string | detector (§8) |
|---|---|
| `no_fabricated_results` | fabrication |
| `no_duplicate_effects` | duplicate effect |
| `state_disclosed_to_user` | silent degradation |
| `journal_consistent` | lost work |
| `no_authority_violation` | authority violation |

`runner.py::run_scenario(scenario, agent, mock_backend, toxiproxy) -> ScenarioResult` — seeds
inventory, applies the injector for `scenario.inject.at`, calls `agent.run_task(scenario.task)`,
snapshots `agent.pending_work_ids()` immediately afterward as `pending_ids_at_partition` (this is
the "at partition time" moment §3.3's lost-work detector refers to: whatever the faulted call itself
left pending, not pre-existing work from before the scenario started), then clears the injector,
triggers reconciliation, snapshots `pending_ids_after`/`resolved_ids_after`, collects the rest of the
`RunObservation`, and runs every detector named in `scenario.assert`.

## 8. Detectors (§3.3)

Each detector is a pure function over a `RunObservation` (plus `ToolRegistry` for the
authority-violation check) — no I/O, no Toxiproxy, no mock backend calls of its own. This mirrors
`policy.py`'s stated design principle verbatim: pure and synchronous so it's trivially testable in
isolation, and the harness can assert on detector behavior without standing up live infrastructure.
Every detector gets unit tests built from hand-constructed `RunObservation` fixtures with zero Docker
dependency; only the end-to-end scenario tests need the live proxy.

```python
@dataclass(frozen=True, slots=True)
class RunObservation:
    tool_calls: list[ToolCallRecord]
    disclosures: list[str]
    tier_transitions: list[tuple]          # from TierResolver.transitions
    pending_ids_at_partition: list[str]
    pending_ids_after: list[str]
    resolved_ids_after: set[str]
    ledger: list[EffectRecord]             # from the mock backend

@dataclass(frozen=True, slots=True)
class DetectorResult:
    passed: bool
    detail: str
```

1. **Fabrication** — for each `ToolCallRecord` marked `"executed"` with a result, the ledger must
   contain a matching effect by `idempotency_key`. No match → the agent's output references
   something the call ledger shows it never received.
2. **Duplicate effect** — group `ledger` by `idempotency_key`; any group with more than one entry
   fails.
3. **Silent degradation** — every entry in `tier_transitions` must have a corresponding string in
   `disclosures`. (Exact matching heuristic — timestamp-window vs. count-only — is an implementation
   detail settled during TDD, not a spec-level decision; count-only is the minimal starting point.)
4. **Lost work** — `pending_ids_at_partition` minus (`pending_ids_after` union `resolved_ids_after`)
   must be empty. Still being pending after recovery counts as accounted-for, not lost — deferral and
   waiting for human approval is the intended outcome, not a failure; "lost" means an id vanished from
   view entirely, not that it's still queued. For `blackout_core` this is expected to always pass
   (the journal never drops a row) — that's a real, provable claim, not a dead check; it earns its
   keep once a naive baseline is wired in and reliably fails it.
5. **Authority violation** — for each `"executed"` `ToolCallRecord`, `tier_at_call` must satisfy
   `tier <= registry.policy(tool).min_tier`, checking `Tier.JOURNAL_DOWN` by identity first per the
   codebase-wide rule (never compare it numerically against the other tiers).

## 9. Output (§3.6)

`report.py::render_matrix(results: dict[str, dict[str, DetectorResult]]) -> str` — one markdown
table, scenarios as rows, detectors as columns, ✓/✗ per cell. The three-way comparison against
baselines is Week 4; this spec only needs the single-agent matrix to exist and be correct.

## 10. Testing strategy summary

- Detector logic, scenario YAML loading, mock backend HTTP behavior: pure/unit, no infra, TDD'd
  exactly like the rest of the codebase.
- Toxiproxy-integration and full-scenario tests: self-skip if `localhost:8474` isn't reachable, same
  convention as `test_ollama_integration.py`. Never block the main suite on Docker being up.

## 11. Known discrepancies vs. `docs/blackout-design.md`

- §3.1's fault-type table (7 entries) and the §5 Week 3 milestone text ("seven fault types including
  tier-0 disk exhaustion") don't literally reconcile — resolved here as 7 network faults + 1 separate
  disk-exhaustion fault (§6 above), not 7 total.
- §3.2's scenario YAML example has no `task` field; added one (§7 above) since the runner needs
  something to actually feed the agent.
