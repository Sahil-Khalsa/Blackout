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
pip install -e ".[dev]"          # editable install; required before imports/tests resolve
python scripts/smoke.py          # manual end-to-end smoke test of policy engine + journal
pytest                           # test suite (tests/ — no tests written yet)
pytest tests/test_x.py::test_y   # single test, once tests exist
```

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

### Known gap between design doc and implementation

`docs/blackout-design.md` §4 names Pydantic as the policy-schema library; the actual implementation uses stdlib `dataclasses` (`@dataclass(frozen=True, slots=True)`) throughout `policy.py` and `journal.py`, and there is no pydantic dependency in `pyproject.toml`. Follow the code, not that line of the doc, unless asked to migrate.
