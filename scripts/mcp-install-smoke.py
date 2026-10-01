"""Real Streamable HTTP initialize/list/call checks; synthetic install fixtures only."""

import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main():
    calls = {
        "get_daily_health": {"target_date": "2020-01-01"},
        "get_health_range": {"start_date": "2020-01-01", "end_date": "2020-01-01"},
        "get_today_readiness": {},
        "get_recent_runs": {},
        "get_recent_activities": {},
        "get_activity": {"activity_id": 42},
        "get_vo2max_history": {},
        "get_hrv_history": {},
        "get_sleep_history": {},
        "get_resting_hr_history": {},
        "get_training_load": {},
        "request_sync": {},
        "get_sync_status": {},
        "get_activity_streams": {"activity_id": 42},
        "get_activity_laps": {"activity_id": 42},
        "get_activity_fit_metrics": {"activity_id": 42},
        "save_activity_feedback": {
            "activity_id": 42,
            "feedback": {"breathing_effort": 0},
            "idempotency_key": "install-fixture-1",
            "expected_revision": 0,
        },
        "get_activity_feedback": {"activity_id": 42},
        "get_activity_weather": {"activity_id": 42},
        "get_training_profile": {},
    }
    legacy = "--legacy" in sys.argv
    if legacy:
        calls = dict(list(calls.items())[:13])
    sizes = {}
    async with (
        streamable_http_client("http://127.0.0.1:8000/mcp") as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = (await session.list_tools()).tools
        assert {t.name for t in tools} == set(calls), f"Expected {len(calls)} tools"
        for tool in tools:
            assert tool.annotations.readOnlyHint == (
                tool.name not in ("request_sync", "save_activity_feedback")
            )
        for name, arguments in calls.items():
            result = await session.call_tool(name, arguments)
            assert not result.isError, f"{name}: {result}"
            payload = result.structuredContent
            assert payload is not None, f"{name}: missing structured output"
            sizes[name] = len(json.dumps(payload).encode())
            assert sizes[name] < 3 * 1024 * 1024, f"{name}: unbounded response"
            if name == "get_activity_fit_metrics":
                assert payload["data"]["recorded_recovery_hr"][0]["value"] == 120
            if name == "get_activity_weather":
                assert payload["data"]["observations"][0]["values"]["temperature"] == 0
            if name == "get_training_profile":
                assert payload["data"]["configured"]["running"][0]["maximum_hr"] == 190
            if name == "get_activity_feedback":
                assert payload["data"]["feedback"]["breathing_effort"] == 0
        if not legacy:
            repeated = await session.call_tool(
                "save_activity_feedback", calls["save_activity_feedback"]
            )
            assert not repeated.isError
    print(
        json.dumps(
            {
                "protocol": "Streamable HTTP initialize/list/call",
                "tools": len(calls),
                "response_bytes": sizes,
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
