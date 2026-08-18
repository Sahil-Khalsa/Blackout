"""OpenAIBackend, unit-tested against a mocked client -- no network or API
key required."""

from unittest.mock import MagicMock

import pytest

from blackout_core import StructuralFailure, Tier
from blackout_core.backends.openai_backend import OpenAIBackend


def _mock_response(tool_name: str, args_json: str) -> MagicMock:
    call = MagicMock()
    call.function.name = tool_name
    call.function.arguments = args_json
    message = MagicMock()
    message.tool_calls = [call]
    resp = MagicMock()
    resp.choices = [MagicMock(message=message)]
    return resp


def test_openai_backend_returns_tool_call(registry):
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_response(
        "page_oncall", '{"oncall": "sre-jane"}'
    )
    backend = OpenAIBackend(registry, client=client)

    tools = registry.available_at(Tier.CLOUD)
    call = backend.propose(tools, Tier.CLOUD, "page the oncall")

    assert call.tool == "page_oncall"
    assert call.args == {"oncall": "sre-jane"}


def test_openai_backend_rejects_tool_outside_offered_set(registry):
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_response(
        "page_oncall", '{"oncall": "sre-jane"}'
    )
    backend = OpenAIBackend(registry, client=client)

    tools = registry.available_at(Tier.LOCAL)  # page_oncall not offered here
    with pytest.raises(StructuralFailure, match="outside the offered set"):
        backend.propose(tools, Tier.LOCAL, "page the oncall")
