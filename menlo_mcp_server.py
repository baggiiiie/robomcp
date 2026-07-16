"""Preserve the original executable/import path for the split Menlo MCP server."""

from menlo_mcp import MenloRobotController
from menlo_mcp.models import number as _number
from menlo_mcp.server import controller, mcp

__all__ = [
    "MenloRobotController",
    "_number",
    "controller",
    "mcp",
]


if __name__ == "__main__":
    mcp.run(transport="stdio")
