"""blackout-chaos: network partition fault injection and behavioral scoring
(docs/blackout-design.md §3).

Stays import-light like blackout_core/backends/__init__.py: pieces needing
the `chaos` extra (requests for toxiproxy_client.py, pyyaml for scenario.py
and -- since it consumes Scenario -- runner.py) are imported explicitly, not
re-exported here, so `import blackout_chaos` works without the extra
installed.
"""

from .agent import ChaosAgent, CoreAgentAdapter, ToolCallRecord, build_mock_backend_registry
from .detectors import (
    DetectorResult,
    RunObservation,
    detect_authority_violation,
    detect_duplicate_effect,
    detect_fabrication,
    detect_lost_work,
    detect_silent_degradation,
)
from .injection import (
    disk_exhausted,
    flapping,
    mid_plan,
    partial_response,
    post_request_pre_response,
    pre_plan,
    recovery_storm,
    slow_success,
)
from .mock_backend import EffectRecord, MockBackendServer
from .report import render_matrix

__all__ = [
    "ChaosAgent",
    "CoreAgentAdapter",
    "DetectorResult",
    "EffectRecord",
    "MockBackendServer",
    "RunObservation",
    "ToolCallRecord",
    "build_mock_backend_registry",
    "detect_authority_violation",
    "detect_duplicate_effect",
    "detect_fabrication",
    "detect_lost_work",
    "detect_silent_degradation",
    "disk_exhausted",
    "flapping",
    "mid_plan",
    "partial_response",
    "post_request_pre_response",
    "pre_plan",
    "recovery_storm",
    "render_matrix",
    "slow_success",
]
