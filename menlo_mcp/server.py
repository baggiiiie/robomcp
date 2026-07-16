"""Register the controller operations as public FastMCP tools."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP, Image

from .controller import MenloRobotController


controller = MenloRobotController()


@asynccontextmanager
async def lifespan(_: FastMCP) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await controller.shutdown()


mcp = FastMCP("Menlo Robot", lifespan=lifespan)


@mcp.tool()
async def start_robot(name: str = "MCP Learning Robot") -> dict[str, Any]:
    """Create a temporary Menlo robot. Open viewer_url before other calls."""
    return await controller.start(name)


@mcp.tool()
async def stop_robot() -> dict[str, Any]:
    """Disconnect and permanently delete this server's temporary robot."""
    return await controller.shutdown()


@mcp.tool()
async def get_scene() -> dict[str, Any]:
    """List scene entities, poses, visibility, attachment, and state."""
    return await controller.scene()


@mcp.tool()
async def get_robot_state() -> dict[str, Any]:
    """Read the robot pose, status, held entities, and active action."""
    return await controller.robot_state()


@mcp.tool()
async def look(
    yaw_degrees: float | None = None,
    pitch_degrees: float | None = None,
) -> Image:
    """Optionally aim and capture JPEG; pitch is -40° up to +20° down."""
    return Image(data=await controller.look(yaw_degrees, pitch_degrees), format="jpeg")


@mcp.tool()
async def walk(
    forward_speed: float,
    lateral_speed: float = 0.0,
    seconds: float = 1.0,
) -> dict[str, Any]:
    """Walk at body-frame speeds in m/s; positive is forward/left."""
    return await controller.walk(forward_speed, lateral_speed, seconds)


@mcp.tool()
async def turn(turn_speed: float, seconds: float = 1.0) -> dict[str, Any]:
    """Turn while stepping; positive turn_speed turns left."""
    return await controller.turn(turn_speed, seconds)


@mcp.tool()
async def go_to(entity_id: str) -> dict[str, Any]:
    """Navigate to an exact scene entity ID."""
    return await controller.go_to(entity_id)


@mcp.tool(name="stop")
async def stop_action() -> dict[str, Any]:
    """Cancel the robot's active runtime action."""
    return await controller.cancel()


@mcp.tool()
async def pick(entity_id: str = "cube") -> dict[str, Any]:
    """Pick an entity; 'cube' asks for the nearest cube."""
    return await controller.pick(entity_id)


@mcp.tool()
async def place(entity_id: str, allow_recycle: bool = False) -> dict[str, Any]:
    """Place and verify an object; source recycling requires explicit opt-in."""
    return await controller.place(entity_id, allow_recycle=allow_recycle)


@mcp.tool()
async def menlo_execute(code: str) -> dict[str, Any]:
    """Execute a bounded plan using state, motion, pick, and place tools."""
    return await controller.execute_code(code)
