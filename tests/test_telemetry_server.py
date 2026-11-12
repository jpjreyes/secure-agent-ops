"""The telemetry MCP server over a real stdio connection: every tool is listed, read-only, and answers."""

import os
import sys
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SNAP = Path(__file__).resolve().parents[1] / "data/itbench-aa/snapshots/Scenario-24"
pytestmark = pytest.mark.skipif(not (SNAP / ".done").exists(), reason="dev snapshot not ingested")


async def _session_run():
    params = StdioServerParameters(command=sys.executable, args=["-m", "sao.mcp.telemetry_server"],
                                   env={**os.environ, "SAO_SNAPSHOT": str(SNAP)})
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        tools = (await s.list_tools()).tools
        res = await s.call_tool("changes", {"kind": "Deployment"})
        return tools, res


def test_tools_listed_read_only_and_answer():
    tools, res = anyio.run(_session_run)
    names = {t.name for t in tools}
    assert {"overview", "alerts", "events", "changes", "object_diff", "log_errors", "trace_summary"} <= names
    assert all(t.annotations and t.annotations.read_only_hint for t in tools)
    text = res.content[0].text
    assert "checkout" in text and "KAFKA_ADDR" in text


def test_concurrent_calls_on_one_server_all_succeed():
    """Behind the gateway several agents share one telemetry server; parallel calls must not break each other."""
    import asyncio
    import sys
    from pathlib import Path

    import pytest
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    snap = Path(__file__).resolve().parents[1] / "data/itbench-aa/snapshots/Scenario-24"
    if not snap.exists():
        pytest.skip("dev snapshot not ingested")

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "sao.mcp.telemetry_server"],
                                       env={"SAO_SNAPSHOT": str(snap)})
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            calls = [("overview", {}), ("changes", {"limit": 60}), ("trace_summary", {}),
                     ("log_errors", {"service": "checkout"}), ("events", {"name_contains": "flagd"}),
                     ("metric_anomalies", {}), ("dependencies", {}), ("alerts", {})] * 3
            return await asyncio.gather(*[s.call_tool(n, a) for n, a in calls])

    results = asyncio.run(go())
    assert [r.is_error for r in results] == [False] * len(results)
