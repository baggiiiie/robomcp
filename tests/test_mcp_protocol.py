from __future__ import annotations

import sys
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class MCPProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_advertises_the_expected_tools(self):
        root = Path(__file__).resolve().parents[1]
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[str(root / "menlo_mcp_server.py")],
            cwd=str(root),
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()

                before_start = await session.call_tool("get_robot_state", {})
                code_result = await session.call_tool(
                    "menlo_execute", {"code": 'return {"ok": True}'}
                )

        names = {tool.name for tool in result.tools}
        self.assertEqual(
            names,
            {
                "start_robot",
                "stop_robot",
                "get_scene",
                "get_robot_state",
                "look",
                "walk",
                "turn",
                "go_to",
                "stop",
                "pick",
                "place",
                "menlo_execute",
            },
        )
        tools = {tool.name: tool for tool in result.tools}
        self.assertEqual(tools["walk"].inputSchema["required"], ["forward_speed"])
        self.assertEqual(
            tools["walk"].inputSchema["properties"]["lateral_speed"]["default"],
            0.0,
        )
        self.assertEqual(tools["go_to"].inputSchema["required"], ["entity_id"])
        self.assertEqual(
            tools["place"].inputSchema["properties"]["allow_recycle"]["default"],
            False,
        )
        self.assertEqual(tools["menlo_execute"].inputSchema["required"], ["code"])
        self.assertTrue(before_start.isError)
        self.assertIn("Call start_robot first", before_start.content[0].text)
        self.assertFalse(code_result.isError)
        self.assertIn('"status": "done"', code_result.content[0].text)


if __name__ == "__main__":
    unittest.main()
