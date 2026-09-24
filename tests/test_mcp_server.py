"""MCP Server 集成测试：InMemory 传输，不涉及 LLM 调用。"""

import asyncio

from mcp import Client
from mcp.client._memory import InMemoryTransport

from insight_agent.mcp_server import server


def _flow():
    async def inner():
        async with Client(server) as client:
            listing = await client.list_tools()
            names = {t.name for t in listing.tools}
            result = await client.call_tool("get_notes", {"topic": "不存在的主题XYZ"})
            archives = await client.call_tool("list_archives", {})
            return names, result, archives

    return asyncio.run(inner())


def test_mcp_tools_registered():
    names, _, _ = _flow()
    assert {"research", "get_notes", "list_archives"} <= names


def test_get_notes_missing_topic():
    _, result, _ = _flow()
    assert "尚无研究档案" in result.content[0].text


def test_list_archives_returns_json():
    _, _, archives = _flow()
    assert "topic" in archives.content[0].text or "尚无研究档案" in archives.content[0].text
