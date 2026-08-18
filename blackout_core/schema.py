"""JSON Schema generation from tool function signatures.

Kept separate from the router so it's unit-testable without any model
backend. Annotations are required, not inferred: a tool registered without a
type hint on every parameter fails loudly at schema-build time rather than
silently degrading to a permissive schema a constrained decoder would accept
anything into.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence
from typing import Any, get_type_hints

from .policy import Tier, ToolPolicy, ToolRegistry

_JSON_TYPE = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


def args_schema_for(fn: Callable[..., Any]) -> dict[str, Any]:
    """JSON Schema for fn's parameters: an object with one property per
    parameter, all required (tools don't currently support optional args),
    additionalProperties forbidden so the model can't smuggle extra fields."""
    sig = inspect.signature(fn)
    hints = get_type_hints(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name in sig.parameters:
        if name not in hints:
            raise ValueError(
                f"{fn.__qualname__!r} parameter {name!r} has no type annotation -- "
                "constrained decoding needs a JSON-schema type for every argument"
            )
        py_type = hints[name]
        if py_type not in _JSON_TYPE:
            raise ValueError(
                f"{fn.__qualname__!r} parameter {name!r} has unsupported type "
                f"{py_type!r} -- supported types: str, int, float, bool"
            )
        properties[name] = {"type": _JSON_TYPE[py_type]}
        required.append(name)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def tool_call_schema(registry: ToolRegistry, tools: Sequence[ToolPolicy]) -> dict[str, Any]:
    """Discriminated-union schema: oneOf a per-tool object with the tool name
    pinned via `const` and args constrained per that tool's signature.

    This is the schema a constrained local-model backend compiles against.
    Building it from `tools` (the caller-supplied *offered* set, normally
    `registry.available_at(tier)`) rather than every registered tool is the
    whole point -- a tool the current tier isn't authorized for is absent
    from the union, not merely discouraged.
    """
    variants = [
        {
            "type": "object",
            "properties": {
                "tool": {"const": policy.name},
                "args": args_schema_for(registry.get(policy.name).fn),
            },
            "required": ["tool", "args"],
            "additionalProperties": False,
        }
        for policy in tools
    ]
    if not variants:
        return {"type": "object", "properties": {}, "additionalProperties": False}
    if len(variants) == 1:
        return variants[0]
    return {"oneOf": variants}


def flat_tool_call_schema(registry: ToolRegistry, tools: Sequence[ToolPolicy]) -> dict[str, Any]:
    """Fallback shape for decoders that don't reliably constrain `oneOf`:
    tool name is a flat `enum` (structurally enforceable everywhere), and
    `args` is left as a generic object for the caller to validate in Python
    per-tool after generation. A validation failure here must be treated as
    a structural failure -- demote to tier 3, per the design doc's stated
    fallback path -- not retried as free-form text.
    """
    names = [policy.name for policy in tools]
    return {
        "type": "object",
        "properties": {
            "tool": {"enum": names} if names else {"type": "string"},
            "args": {"type": "object"},
        },
        "required": ["tool", "args"],
        "additionalProperties": False,
    }
