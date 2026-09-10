from __future__ import annotations

import asyncio

from garmin_health_gateway.mcp_server import mcp


def test_query_tools_are_read_only_and_sync_is_annotated_as_mutating() -> None:
    tools = asyncio.run(mcp.list_tools())
    assert len(tools) >= 12
    for tool in tools:
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is (tool.name != "request_sync")
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.openWorldHint is (tool.name == "request_sync")


def test_required_mcp_tools_exist() -> None:
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert {
        "get_daily_health",
        "get_health_range",
        "get_today_readiness",
        "get_recent_runs",
        "get_recent_activities",
        "get_activity",
        "get_vo2max_history",
        "get_hrv_history",
        "get_sleep_history",
        "get_resting_hr_history",
        "get_training_load",
        "request_sync",
        "get_sync_status",
    } <= names
