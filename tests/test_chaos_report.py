"""Pure tests for report.py -- hand-built detector-result dicts, no infra."""

from blackout_chaos.detectors import DetectorResult
from blackout_chaos.report import render_matrix


def test_render_matrix_empty_when_no_results():
    assert render_matrix({}) == "(no scenarios run)"


def test_render_matrix_shows_check_for_passed_and_cross_for_failed():
    results = {
        "write_ack_lost": {
            "fabrication": DetectorResult(True, ""),
            "duplicate_effect": DetectorResult(False, "duplicate found"),
        }
    }
    table = render_matrix(results)
    lines = table.splitlines()
    assert lines[0] == "| scenario | fabrication | duplicate_effect |"
    assert lines[1] == "|---|---|---|"
    assert lines[2] == "| write_ack_lost | ✓ | ✗ |"


def test_render_matrix_handles_multiple_scenarios_with_different_detector_sets():
    results = {
        "scenario_a": {"fabrication": DetectorResult(True, "")},
        "scenario_b": {"lost_work": DetectorResult(True, "")},
    }
    table = render_matrix(results)
    header = table.splitlines()[0]
    assert "fabrication" in header
    assert "lost_work" in header
