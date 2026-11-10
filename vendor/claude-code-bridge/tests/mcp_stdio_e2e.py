"""Offline stdio MCP integration; no real provider messages are sent."""
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


async def main():
    params = StdioServerParameters(command=sys.executable, args=["-m", "agent_adaptors.server", "--config", str(ROOT / "examples_fake.json")], cwd=str(ROOT))
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as client:
            await client.initialize()
            tools = await client.list_tools()
            assert len(tools.tools) == 10
            async def call(tool_name, **kwargs):
                result = await client.call_tool(tool_name, kwargs)
                assert not result.isError, result
                return result.structuredContent or json.loads(result.content[0].text)
            runtime = await call("agent_runtime_status")
            assert runtime["status"] == "degraded"
            session = await call("agent_start_session", adaptor_id="one", name="MCP offline test")
            assert session["id"].startswith("one:")
            stream = await call("agent_start_stream_message", session_id=session["id"], message="hello")
            for _ in range(100):
                row = await call("agent_poll_stream", stream_id=stream["streamId"])
                if row["status"] not in {"starting", "running"}:
                    break
                await asyncio.sleep(0.01)
            assert row["status"] == "completed", row
            assert row["reply"] == "Fake: hello", row
            read = await call("agent_read_session", session_id=session["id"])
            assert read["messages"][-1]["text"] == "Fake: hello"
            print("MCP stdio e2e passed: 10 tools, degraded isolation, create/stream/poll/read")


if __name__ == "__main__":
    asyncio.run(main())

