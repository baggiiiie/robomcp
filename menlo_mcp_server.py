"""MCP server exposing intuitive controls for a Menlo SimpleSim robot."""

from __future__ import annotations

import asyncio
import math
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP, Image
from menlo_robot_sdk import AsyncClient, connect
from menlo_robot_sdk.connection import MenloSession
from menlo_robot_sdk.experimental import generate_room_key


RCS_URL = "https://api.menlo.ai/rcs"
VIEWER_BASE_URL = "https://sim.menlo.ai"
MODEL = "asimov-v0"


def _number(name: str, value: float, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    value = float(value)
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _json(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    return value


class MenloRobotController:
    """Own one temporary Menlo robot for the lifetime of this MCP process."""

    def __init__(self) -> None:
        self.client: AsyncClient | None = None
        self.session: MenloSession | None = None
        self.robot_id: str | None = None
        self.viewer_url: str | None = None
        self._skills: set[str] = set()
        self._lifecycle_lock = asyncio.Lock()

    async def start(self, name: str = "MCP Learning Robot") -> dict[str, Any]:
        """Create and connect a robot, returning the SimpleSim viewer link."""
        async with self._lifecycle_lock:
            if self.session is not None:
                return {
                    "status": "already_running",
                    "robot_id": self.robot_id,
                    "viewer_url": self.viewer_url,
                    "next_step": "Keep the viewer open and visible, then call get_scene.",
                }

            load_dotenv(Path(__file__).with_name(".env"))
            api_key = os.getenv("MENLO_API_KEY", "").strip()
            if not api_key:
                raise RuntimeError(
                    "MENLO_API_KEY is missing. Copy .env.example to .env and add your key."
                )

            client = AsyncClient(rcs_url=RCS_URL, api_key=api_key)
            robot_id: str | None = None
            session: MenloSession | None = None
            try:
                created = await client.robots.create(name=name, model=MODEL)
                robot_id = created.robot.id
                session = await connect(
                    client,
                    robot_id,
                    worker_names=[],
                    rcw_identity_prefix="simplesim",
                    join_livekit=True,
                )
                room_key = await generate_room_key(client, robot_id)
            except BaseException:
                if session is not None:
                    try:
                        await session.disconnect(delete_session=True)
                    except Exception:
                        pass
                if robot_id is not None:
                    try:
                        await client.robots.delete(robot_id)
                    except Exception:
                        pass
                try:
                    await client.aclose()
                except Exception:
                    pass
                raise

            self.client = client
            self.session = session
            self.robot_id = robot_id
            self.viewer_url = f"{VIEWER_BASE_URL}/?key={room_key}"
            self._skills.clear()
            return {
                "status": "started",
                "robot_id": robot_id,
                "viewer_url": self.viewer_url,
                "next_step": (
                    "Open viewer_url in Chrome and keep it visible. Wait for the scene "
                    "to load, then call get_scene."
                ),
            }

    async def shutdown(self) -> dict[str, Any]:
        """Disconnect, delete the temporary robot, and close the API client."""
        async with self._lifecycle_lock:
            if self.session is None and self.client is None:
                return {"status": "not_running"}

            session, client, robot_id = self.session, self.client, self.robot_id
            self.session = None
            self.client = None
            self.robot_id = None
            self.viewer_url = None
            self._skills.clear()

            errors: list[str] = []
            if session is not None:
                try:
                    await session.disconnect(delete_session=True)
                except Exception as exc:
                    errors.append(f"disconnect: {type(exc).__name__}: {exc}")
            if client is not None and robot_id is not None:
                try:
                    await client.robots.delete(robot_id)
                except Exception as exc:
                    errors.append(f"delete robot: {type(exc).__name__}: {exc}")
            if client is not None:
                try:
                    await client.aclose()
                except Exception as exc:
                    errors.append(f"close client: {type(exc).__name__}: {exc}")

            result: dict[str, Any] = {"status": "stopped", "robot_id": robot_id}
            if errors:
                result["status"] = "stopped_with_warnings"
                result["cleanup_warnings"] = errors
            return result

    def _require_session(self) -> MenloSession:
        if self.session is None:
            raise RuntimeError("Robot is not running. Call start_robot first.")
        return self.session

    async def _require_skill(self, name: str, timeout_s: float = 120) -> MenloSession:
        session = self._require_session()
        if name in self._skills:
            return session

        deadline = time.monotonic() + timeout_s
        while True:
            skills = await session.discover_skills()
            self._skills = {skill.name for skill in skills}
            if name in self._skills:
                return session
            if time.monotonic() >= deadline:
                available = ", ".join(sorted(self._skills)) or "none"
                raise RuntimeError(
                    f"Menlo skill {name!r} is unavailable (found: {available}). "
                    "Make sure the viewer is open, visible, and fully loaded."
                )
            await asyncio.sleep(1)

    async def robot_state(self) -> dict[str, Any]:
        state = await self._require_session().state.get("robot_status")
        return _json(state)

    async def scene(self) -> dict[str, Any]:
        scene = await self._require_session().state.get("scene_state")
        data = _json(scene)
        data["entity_count"] = len(data.get("entities", {}))
        return data

    async def camera(self) -> bytes:
        return await self._require_session().get_vision("pov")

    async def _validate_entity(
        self, entity_id: str, *, allow_cube_alias: bool = False
    ) -> None:
        if not entity_id or not entity_id.strip():
            raise ValueError("entity_id must not be empty")
        if allow_cube_alias and entity_id == "cube":
            return
        scene = await self._require_session().state.get("scene_state")
        entities = scene.entities
        if entity_id not in entities and not any(
            entity.entity_id == entity_id for entity in entities.values()
        ):
            choices = sorted(
                {key for key in entities}
                | {entity.entity_id for entity in entities.values()}
            )
            preview = ", ".join(choices[:20])
            raise ValueError(
                f"Unknown entity_id {entity_id!r}. Scene entities include: {preview}"
            )

    async def _invoke(
        self, skill: str, parameters: dict[str, Any], *, timeout_s: float
    ) -> dict[str, Any]:
        session = await self._require_skill(skill)
        result = await session.invoke(skill, parameters, timeout_s=timeout_s)
        response = {"action": skill, "result": _json(result)}
        try:
            response["robot_state"] = await self.robot_state()
        except Exception as exc:
            response["state_warning"] = f"{type(exc).__name__}: {exc}"
        return response

    async def walk(
        self, forward_speed: float, lateral_speed: float, seconds: float
    ) -> dict[str, Any]:
        return await self._invoke(
            "set_velocity",
            {
                "vx": _number("forward_speed", forward_speed, -1.5, 1.5),
                "vy": _number("lateral_speed", lateral_speed, -1.5, 1.5),
                "wz": 0.0,
                "duration_s": _number("seconds", seconds, 0.1, 10.0),
            },
            timeout_s=30,
        )

    async def turn(self, turn_speed: float, seconds: float) -> dict[str, Any]:
        turn_speed = _number("turn_speed", turn_speed, -0.6, 0.6)
        return await self._invoke(
            "set_velocity",
            {
                "vx": 0.2 if abs(turn_speed) > 0.05 else 0.0,
                "vy": 0.0,
                "wz": turn_speed,
                "duration_s": _number("seconds", seconds, 0.1, 10.0),
            },
            timeout_s=30,
        )

    async def go_to(self, entity_id: str) -> dict[str, Any]:
        await self._validate_entity(entity_id)
        return await self._invoke(
            "go_to",
            {"target": {"kind": "entity", "entity_id": entity_id}},
            timeout_s=300,
        )

    async def cancel(self) -> dict[str, Any]:
        return await self._invoke("cancel", {}, timeout_s=15)

    async def pick(self, entity_id: str) -> dict[str, Any]:
        await self._validate_entity(entity_id, allow_cube_alias=True)
        return await self._invoke(
            "pick_entity",
            {"target": {"kind": "entity", "entity_id": entity_id}},
            timeout_s=60,
        )

    async def place(self, entity_id: str) -> dict[str, Any]:
        await self._validate_entity(entity_id)
        return await self._invoke(
            "place_entity",
            {"target": {"kind": "entity", "entity_id": entity_id}},
            timeout_s=60,
        )

    async def aim_head(
        self, yaw_degrees: float | None, pitch_degrees: float | None
    ) -> dict[str, Any]:
        if yaw_degrees is None and pitch_degrees is None:
            raise ValueError("Provide yaw_degrees, pitch_degrees, or both")
        parameters: dict[str, float] = {}
        if yaw_degrees is not None:
            parameters["yaw"] = math.radians(
                _number("yaw_degrees", yaw_degrees, -80, 80)
            )
        if pitch_degrees is not None:
            parameters["pitch"] = math.radians(
                _number("pitch_degrees", pitch_degrees, -40, 40)
            )
        return await self._invoke("set_head", parameters, timeout_s=15)


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
    """Create a temporary Menlo robot. Open the returned viewer_url before other calls."""
    return await controller.start(name)


@mcp.tool()
async def stop_robot() -> dict[str, Any]:
    """Disconnect and permanently delete the temporary robot created by this server."""
    return await controller.shutdown()


@mcp.tool()
async def get_scene() -> dict[str, Any]:
    """List the simulated scene entities, poses, visibility, attachment, and state."""
    return await controller.scene()


@mcp.tool()
async def get_robot_state() -> dict[str, Any]:
    """Read the robot's pose, status, held entities, and active action."""
    return await controller.robot_state()


@mcp.tool()
async def get_camera() -> Image:
    """Capture the robot's current point-of-view camera as a JPEG image."""
    return Image(data=await controller.camera(), format="jpeg")


@mcp.tool()
async def walk(
    forward_speed: float,
    lateral_speed: float = 0.0,
    seconds: float = 1.0,
) -> dict[str, Any]:
    """Walk at body-frame speeds in m/s. Positive is forward/left; negative is back/right."""
    return await controller.walk(forward_speed, lateral_speed, seconds)


@mcp.tool()
async def turn(turn_speed: float, seconds: float = 1.0) -> dict[str, Any]:
    """Turn while stepping. turn_speed is rad/s: positive turns left, negative turns right."""
    return await controller.turn(turn_speed, seconds)


@mcp.tool()
async def go_to(entity_id: str) -> dict[str, Any]:
    """Navigate to an exact scene entity ID using Menlo's built-in A* route planner."""
    return await controller.go_to(entity_id)


@mcp.tool(name="stop")
async def stop_action() -> dict[str, Any]:
    """Cancel the robot's active movement or other in-flight runtime action."""
    return await controller.cancel()


@mcp.tool()
async def pick(entity_id: str = "cube") -> dict[str, Any]:
    """Pick a reachable entity. The special ID 'cube' asks Menlo for the nearest cube."""
    return await controller.pick(entity_id)


@mcp.tool()
async def place(entity_id: str) -> dict[str, Any]:
    """Place the held object at an exact scene entity or zone ID."""
    return await controller.place(entity_id)


@mcp.tool()
async def aim_head(
    yaw_degrees: float | None = None,
    pitch_degrees: float | None = None,
) -> dict[str, Any]:
    """Aim the head/camera in degrees. Positive yaw is left; positive pitch is down."""
    return await controller.aim_head(yaw_degrees, pitch_degrees)


if __name__ == "__main__":
    mcp.run(transport="stdio")
