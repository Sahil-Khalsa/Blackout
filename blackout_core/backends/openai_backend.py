"""Tier-1 cloud model backend: OpenAI native tool-calling.

Behind the `cloud` optional extra (see pyproject.toml) so blackout_core and
the chaos harness stay runnable without the `openai` package or an API key
present -- only importing this module pulls in that dependency.

Unlike the tier-2 Ollama backend, this is not schema/grammar constrained --
OpenAI's function-calling API doesn't guarantee structural exclusion of
tools outside the offered list the way JSON-schema-forced sampling does.
That's consistent with docs/blackout-design.md §2.9: only tier 2 claims the
"physically cannot emit an unauthorized call" property. Here an
out-of-set tool name is still checked and rejected, but as a runtime
StructuralFailure rather than a structural impossibility.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from openai import APIError, OpenAI

from ..policy import Tier, ToolPolicy, ToolRegistry
from ..router import BackendUnavailable, StructuralFailure, ToolCall
from ..schema import args_schema_for


class OpenAIBackend:
    def __init__(
        self,
        registry: ToolRegistry,
        model: str = "gpt-4o-mini",
        client: OpenAI | None = None,
    ) -> None:
        self.registry = registry
        self.model = model
        self.client = client or OpenAI()

    def propose(
        self, tools: Sequence[ToolPolicy], tier: Tier, task: str
    ) -> ToolCall | None:
        if not tools:
            return None

        schema_tools = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.describe_for(tier),
                    "parameters": args_schema_for(self.registry.get(t.name).fn),
                },
            }
            for t in tools
        ]
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": task}],
                tools=schema_tools,
                tool_choice="auto",
            )
        except APIError as exc:
            raise BackendUnavailable(f"openai request failed: {exc}") from exc

        message = resp.choices[0].message
        if not message.tool_calls:
            return None

        call = message.tool_calls[0]
        offered = {t.name for t in tools}
        if call.function.name not in offered:
            raise StructuralFailure(
                f"openai proposed tool {call.function.name!r} outside the offered set {offered}"
            )
        try:
            args = json.loads(call.function.arguments)
        except json.JSONDecodeError as exc:
            raise StructuralFailure(
                f"openai returned invalid JSON args: {call.function.arguments!r}"
            ) from exc
        return ToolCall(tool=call.function.name, args=args, raw=call.function.arguments)
