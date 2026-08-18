"""Mechanical proof that the tier-2 schema structurally excludes tools the
current tier isn't authorized for -- the claim behind
docs/blackout-design.md §2.9's "physically cannot emit an unauthorized
call." This is the test the design review called out to write in Week 1,
not discover missing in Week 4.
"""

import pytest

from blackout_core import Tier, args_schema_for, tool_call_schema
from blackout_core.schema import flat_tool_call_schema


def test_args_schema_for_basic_types(registry):
    schema = args_schema_for(registry.get("place_restock_order").fn)
    assert schema["properties"]["sku"] == {"type": "string"}
    assert schema["properties"]["qty"] == {"type": "integer"}
    assert schema["required"] == ["sku", "qty", "window"]
    assert schema["additionalProperties"] is False


def test_args_schema_for_requires_annotations():
    def unannotated(sku):
        return sku

    with pytest.raises(ValueError, match="no type annotation"):
        args_schema_for(unannotated)


def test_args_schema_for_rejects_unsupported_types():
    def bad(sku: list) -> None:
        return None

    with pytest.raises(ValueError, match="unsupported type"):
        args_schema_for(bad)


def _allowed_tool_names(schema: dict) -> set[str]:
    variants = schema.get("oneOf", [schema])
    return {v["properties"]["tool"]["const"] for v in variants}


def test_tier2_schema_excludes_cloud_only_refuse_tool(registry):
    tools = registry.available_at(Tier.LOCAL)
    names = {t.name for t in tools}
    assert "page_oncall" not in names  # min_tier=CLOUD, offline_policy=REFUSE

    schema = tool_call_schema(registry, tools)
    allowed = _allowed_tool_names(schema)
    assert "page_oncall" not in allowed
    assert allowed == names


def test_tier1_schema_includes_page_oncall(registry):
    tools = registry.available_at(Tier.CLOUD)
    schema = tool_call_schema(registry, tools)
    assert "page_oncall" in _allowed_tool_names(schema)


def test_flat_schema_enum_matches_offered_set(registry):
    tools = registry.available_at(Tier.LOCAL)
    schema = flat_tool_call_schema(registry, tools)
    assert "page_oncall" not in schema["properties"]["tool"]["enum"]
    assert set(schema["properties"]["tool"]["enum"]) == {t.name for t in tools}
