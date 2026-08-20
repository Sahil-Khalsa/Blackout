"""blackout-chaos: network partition fault injection and behavioral scoring
(docs/blackout-design.md §3).

Stays import-light like blackout_core/backends/__init__.py: pieces needing
the `chaos` extra (requests for toxiproxy_client.py, pyyaml for scenario.py
and -- since it consumes Scenario -- runner.py) are imported explicitly, not
re-exported here, so `import blackout_chaos` works without the extra
installed.
"""

from .agent import ChaosAgent, CoreAgentAdapter, ToolCallRecord, build_mock_backend_registry
from .mock_backend import EffectRecord, MockBackendServer

__all__ = [
    "ChaosAgent",
    "CoreAgentAdapter",
    "EffectRecord",
    "MockBackendServer",
    "ToolCallRecord",
    "build_mock_backend_registry",
]
