# Blackout

An authority runtime for agents that lose connectivity, and the harness that proves it works.

Full design and rationale: [`docs/blackout-design.md`](docs/blackout-design.md).

## Layout

```
blackout_core/   tiered capability runtime, policy engine, intent journal
blackout_chaos/  network partition fault injection and behavioral scoring (not yet implemented)
scripts/         manual verification scripts (smoke.py)
tests/           pytest suite
docs/            design doc
```

## Status

Week 1 of the build plan (docs/blackout-design.md §5): tool registry, policy engine, and
tier resolver are implemented in `blackout_core`. Intent journal (§2.5-2.11) is implemented.
`blackout_chaos`, the reconciler, the model router, and the approval-surface CLI are not yet
built.

## Setup

```
pip install -e ".[dev]"
python scripts/smoke.py
pytest
```
