"""Scenario spec + YAML loader (docs/blackout-design.md §3.2, spec §7).

`task` and `warmup_task` are both necessary additions beyond the doc's own
example YAML. `task` is what the runner feeds run_task() for the main
faulted attempt. `warmup_task`, if set, is fed to run_task() BEFORE the
injector is applied, to prime the read cache: PolicyEngine.evaluate checks
declared preconditions immediately after the tier-0 short-circuit,
unconditionally, before comparing tier at all (policy.py::PolicyEngine.
evaluate) -- so a precondition-bearing tool with a cold cache REFUSEs before
DEFER or EXECUTE is ever reached, silently turning a precondition-bearing
scenario into a vacuous pass without a warmup read.

`seed_inventory` is a third necessary addition: nothing else in the 4-arg
run_scenario signature (spec §7) carries a starting inventory level for the
mock backend, and place_restock_order's "below_threshold" precondition
needs one to mean anything.

Not re-exported from blackout_chaos/__init__.py -- this module needs
pyyaml (the chaos extra), and the package stays import-light without it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True, slots=True)
class InjectSpec:
    at: str
    tool: str
    duration_s: float = 300.0


@dataclass(frozen=True, slots=True)
class Scenario:
    scenario: str
    description: str
    task: str
    inject: InjectSpec
    assert_: list[str]
    warmup_task: str | None = None
    seed_inventory: dict[str, int] = field(default_factory=dict)


def _load_one(data: dict) -> Scenario:
    inject = InjectSpec(**data["inject"])
    return Scenario(
        scenario=data["scenario"],
        description=data["description"],
        task=data["task"],
        inject=inject,
        assert_=list(data["assert"]),
        warmup_task=data.get("warmup_task"),
        seed_inventory=dict(data.get("seed_inventory", {})),
    )


def load_scenario(path: str | Path) -> Scenario:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return _load_one(data)


def load_all_scenarios(directory: str | Path) -> list[Scenario]:
    directory = Path(directory)
    return [load_scenario(p) for p in sorted(directory.glob("*.yaml"))]
