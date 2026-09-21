"""Regression budgets include cold discovery and both representative workflows."""

import pytest

from uoft_mcp.benchmark import measure


@pytest.mark.anyio
@pytest.mark.parametrize("workflow", ["courses", "academic_records"])
async def test_efficiency_budgets(workflow):
    legacy = await measure("legacy", workflow)
    compact = await measure("compact", workflow)
    assert compact["tool_count"] == 7
    assert compact["tool_definition_bytes"] <= legacy["tool_definition_bytes"] * 0.5
    assert compact["total_response_bytes"] <= legacy["total_response_bytes"] * 0.25
    assert compact["discovery_response_bytes"] > 0
    assert compact["upstream_requests"] == legacy["upstream_requests"] == 3
    assert compact["paging_upstream_requests"] == 0
    # Compact includes discovery, one batch, and an extra snapshot page. Legacy
    # fetches three full responses; both workflows use three tool round trips.
    assert compact["mcp_tool_calls"] == legacy["mcp_tool_calls"] == 3
