# Blackout

An authority runtime for agents that lose connectivity, and the harness that proves it works.

Full design and rationale: [`docs/blackout-design.md`](docs/blackout-design.md). Current
implementation status, component by component: [`STATUS.md`](STATUS.md).

## Layout

```
blackout_core/            tiered capability runtime, policy engine, intent journal, model router
blackout_core/backends/   concrete tier-1 (OpenAI) and tier-2 (Ollama) model backends
blackout_chaos/           network partition fault injection and behavioral scoring (not yet implemented)
scripts/                  manual verification scripts (smoke.py)
tests/                    pytest suite
docs/                     design doc
```

## Status

Week 1 of the build plan (`docs/blackout-design.md` §5) is done: tool registry, policy engine,
tier resolver, intent journal, model router (tier-1 OpenAI native tool-calling, tier-2 Ollama
JSON-schema-constrained generation, tier-3 deterministic rules), and a minimal agent loop.

Ollama's schema constraint was verified live to structurally exclude tools outside the offered
set — a tool a tier isn't authorized for is absent from the compiled schema, not just discouraged.

Week 2 is underway: a read cache now backs the agent loop, so a deferred write's preconditions
come from genuine cached reads with real staleness tracking, not caller-injected test data — the
loop produces a real `DEFER` when the evidence is fresh and correctly `REFUSE`s instead when it's
too stale to responsibly queue. Loop checkpointing is also done: a crash mid-partition now produces
`orphaned` intents (visible, flagged as lacking context) rather than silently replaying or silently
losing them. The reconciler is done too: on reconnect it expires, topologically orders and cascades
dependency rejections, collapses duplicate intents, re-evaluates preconditions against fresh reads
to classify each surviving intent `ready` / `ready_with_drift` / `rejected`, detects intra-batch
resource conflicts, and sorts the result so the shakiest justifications are reviewed first. See
`STATUS.md` for the full write-up.

Not yet built: `blackout_chaos` and the approval-surface CLI.

## Setup

```
pip install -e ".[dev,cloud]"
python scripts/smoke.py
pytest
```

Tier-2 tests need Ollama running locally with `qwen2.5:1.5b` pulled; they skip automatically if
`localhost:11434` isn't reachable. Tier-1 tests are mocked and need no API key. To use a real
cloud backend, copy `.env.example` to `.env` and set `OPENAI_API_KEY` (never commit `.env`).
