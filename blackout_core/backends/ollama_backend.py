"""Tier-2 local model backend: Ollama with JSON-schema-constrained generation.

Uses stdlib urllib.request against Ollama's HTTP API -- the wire protocol is
plain JSON over HTTP, so no dependency is needed. Constrains generation via
Ollama's `format` field (a JSON Schema), which Ollama enforces through
grammar-based sampling. This is the JSON-schema-enforced-sampling path named
as an alternative to hand-rolled GBNF in docs/blackout-design.md §2.9.

Empirically verified (see STATUS.md) that a `oneOf` discriminated union with
per-variant `const` tool names holds under Ollama 0.32 + qwen2.5:1.5b: a tool
excluded from the union cannot be named in the output, even when the prompt
directly asks for it.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Sequence

from ..policy import Tier, ToolPolicy, ToolRegistry
from ..router import BackendUnavailable, StructuralFailure, ToolCall
from ..schema import tool_call_schema

_SYSTEM_PROMPT = (
    "You are a tool-calling agent. Respond with a single JSON object naming "
    "exactly one tool and its arguments. Only the tools listed are available "
    "-- if the task doesn't match any of them, still pick the closest one; "
    "you have no other way to respond."
)


class OllamaBackend:
    def __init__(
        self,
        registry: ToolRegistry,
        model: str,
        base_url: str = "http://localhost:11434",
        timeout_s: float = 30.0,
    ) -> None:
        self.registry = registry
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def propose(
        self, tools: Sequence[ToolPolicy], tier: Tier, task: str
    ) -> ToolCall | None:
        if not tools:
            return None

        schema = tool_call_schema(self.registry, tools)
        descriptions = "\n".join(f"- {t.name}: {t.describe_for(tier)}" for t in tools)
        prompt = f"Available tools:\n{descriptions}\n\nTask: {task}"

        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "format": schema,
                "stream": False,
            }
        ).encode()
        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                payload = json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError) as exc:
            raise BackendUnavailable(f"ollama request failed: {exc}") from exc

        content = payload["message"]["content"]
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise StructuralFailure(
                f"ollama returned non-JSON content despite format constraint: {content!r}"
            ) from exc

        offered = {t.name for t in tools}
        tool_name = parsed.get("tool")
        if tool_name not in offered:
            raise StructuralFailure(
                f"ollama proposed tool {tool_name!r} outside the offered set {offered}"
            )
        return ToolCall(tool=tool_name, args=parsed.get("args", {}), raw=content)
