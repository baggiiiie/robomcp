"""Manage SDK resources and wait for viewer-delayed runtime skill readiness."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any, Awaitable

from dotenv import load_dotenv
from menlo_robot_sdk import AsyncClient, connect
from menlo_robot_sdk.connection import MenloSession
from menlo_robot_sdk.experimental import generate_room_key

from .models import RobotState, Scene, jsonable


RCS_URL = "https://api.menlo.ai/rcs"
VIEWER_BASE_URL = "https://sim.menlo.ai"
MODEL = "asimov-v0"


async def _cleanup(
    session: MenloSession | None,
    client: AsyncClient | None,
    robot_id: str | None,
) -> list[str]:
    errors = []

    async def attempt(label: str, operation: Awaitable[Any]) -> None:
        try:
            await operation
        except Exception as exc:
            errors.append(f"{label}: {type(exc).__name__}: {exc}")

    if session:
        await attempt("disconnect", session.disconnect(delete_session=True))
    if client and robot_id:
        await attempt("delete robot", client.robots.delete(robot_id))
    if client:
        await attempt("close client", client.aclose())
    return errors


class MenloConnection:
    """Owns the SDK resources and translates SDK models into typed payloads."""

    def __init__(self) -> None:
        self.client: AsyncClient | None = None
        self.session: MenloSession | None = None
        self.robot_id: str | None = None
        self.viewer_url: str | None = None
        self._skills: set[str] = set()
        self._lifecycle_lock = asyncio.Lock()

    async def start(self, name: str = "MCP Learning Robot") -> dict[str, Any]:
        async with self._lifecycle_lock:
            if self.session:
                return {
                    "status": "already_running",
                    "robot_id": self.robot_id,
                    "viewer_url": self.viewer_url,
                    "next_step": (
                        "Keep the viewer open and visible, then call get_scene."
                    ),
                }

            load_dotenv(Path(__file__).parents[1] / ".env")
            api_key = os.getenv("MENLO_API_KEY", "").strip()
            if not api_key:
                raise RuntimeError(
                    "MENLO_API_KEY is missing. Copy .env.example to .env and add "
                    "your key."
                )

            client = AsyncClient(rcs_url=RCS_URL, api_key=api_key)
            robot_id = None
            session = None
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
                await _cleanup(session, client, robot_id)
                raise

            self.client, self.session, self.robot_id = client, session, robot_id
            self.viewer_url = f"{VIEWER_BASE_URL}/?key={room_key}"
            self._skills.clear()
            return {
                "status": "started",
                "robot_id": robot_id,
                "viewer_url": self.viewer_url,
                "next_step": (
                    "Open viewer_url and keep it visible, then call get_scene."
                ),
            }

    async def shutdown(self) -> dict[str, Any]:
        async with self._lifecycle_lock:
            if self.session is None and self.client is None:
                return {"status": "not_running"}

            session, client, robot_id = self.session, self.client, self.robot_id
            self.session = self.client = None
            self.robot_id = self.viewer_url = None
            self._skills.clear()

            errors = await _cleanup(session, client, robot_id)
            return {
                "status": "stopped_with_warnings" if errors else "stopped",
                "robot_id": robot_id,
                **({"cleanup_warnings": errors} if errors else {}),
            }

    def _require_session(self) -> MenloSession:
        if self.session is None:
            raise RuntimeError("Robot is not running. Call start_robot first.")
        return self.session

    async def _require_skill(self, name: str, timeout_s: float = 120) -> MenloSession:
        session = self._require_session()
        deadline = time.monotonic() + timeout_s
        while name not in self._skills:
            self._skills = {skill.name for skill in await session.discover_skills()}
            if name in self._skills:
                break
            if time.monotonic() >= deadline:
                available = ", ".join(sorted(self._skills)) or "none"
                raise RuntimeError(
                    f"Menlo skill {name!r} is unavailable (found: {available}). "
                    "Make sure the viewer is open, visible, and fully loaded."
                )
            await asyncio.sleep(1)
        return session

    async def robot_state(self) -> dict[str, Any]:
        return jsonable(await self._require_session().state.get("robot_status"))

    async def _robot_state_model(self) -> RobotState:
        return RobotState.model_validate(await self.robot_state())

    async def _scene_model(self) -> Scene:
        raw = await self._require_session().state.get("scene_state")
        return Scene.model_validate(jsonable(raw))

    async def scene(self) -> dict[str, Any]:
        scene = await self._scene_model()
        data = scene.model_dump(mode="json", exclude_none=True)
        data["entity_count"] = len(scene.entities)
        return data

    async def camera(self) -> bytes:
        return await self._require_session().get_vision("pov")

    async def _invoke(
        self, skill: str, parameters: dict[str, Any], *, timeout_s: float
    ) -> dict[str, Any]:
        session = await self._require_skill(skill)
        result = await session.invoke(skill, parameters, timeout_s=timeout_s)
        return {
            "action": skill,
            "result": jsonable(result),
            "robot_state": await self.robot_state(),
        }
