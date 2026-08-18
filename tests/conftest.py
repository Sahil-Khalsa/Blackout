"""Shared fixtures: a small example tool registry mirroring the tools used
throughout docs/blackout-design.md (read_inventory / place_restock_order /
page_oncall), with type annotations so schema generation works."""

import pytest

from blackout_core import Effect, OfflinePolicy, Tier, ToolRegistry


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()

    @reg.tool(
        effect=Effect.READ,
        min_tier=Tier.RULES,
        offline_policy=OfflinePolicy.EXECUTE,
        tier_descriptions={
            1: "Read the current inventory level for a SKU.",
            2: "Check stock.",
        },
    )
    def read_inventory(sku: str) -> dict:
        return {"sku": sku, "level": 4}

    @reg.tool(
        effect=Effect.EXTERNAL_WRITE,
        min_tier=Tier.CLOUD,
        offline_policy=OfflinePolicy.DEFER,
        idempotency_key=lambda a: f"restock:{a['sku']}:{a['window']}",
        preconditions=["inventory.below_threshold"],
        max_precondition_staleness_s=3600,
        ttl_seconds=14400,
        reversible=False,
    )
    def place_restock_order(sku: str, qty: int, window: str) -> None:
        return None

    @reg.tool(
        effect=Effect.EXTERNAL_WRITE,
        min_tier=Tier.CLOUD,
        offline_policy=OfflinePolicy.REFUSE,
        idempotency_key=lambda a: f"page:{a['oncall']}",
    )
    def page_oncall(oncall: str) -> None:
        return None

    return reg
