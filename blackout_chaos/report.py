"""Markdown scenario x detector matrix (docs/blackout-design.md §3.6, spec
§9). The three-way comparison against baselines is Week 4 -- this only
needs the single-agent matrix to exist and be correct."""

from __future__ import annotations

from .detectors import DetectorResult


def render_matrix(results: dict[str, dict[str, DetectorResult]]) -> str:
    if not results:
        return "(no scenarios run)"

    detector_names: list[str] = []
    for per_scenario in results.values():
        for name in per_scenario:
            if name not in detector_names:
                detector_names.append(name)

    header = "| scenario | " + " | ".join(detector_names) + " |"
    separator = "|---|" + "|".join("---" for _ in detector_names) + "|"
    rows = [header, separator]
    for scenario_name, per_scenario in results.items():
        cells = []
        for name in detector_names:
            result = per_scenario.get(name)
            if result is None:
                cells.append(" ")
            else:
                cells.append("✓" if result.passed else "✗")
        rows.append(f"| {scenario_name} | " + " | ".join(cells) + " |")

    return "\n".join(rows)
