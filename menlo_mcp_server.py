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

from menlo_code_mode import MenloCodeExecutor, PlanActionError


RCS_URL = "https://api.menlo.ai/rcs"
VIEWER_BASE_URL = "https://sim.menlo.ai"
MODEL = "asimov-v0"

HEAD_PITCH_MIN_DEGREES = -40.0
HEAD_PITCH_MAX_DEGREES = 20.0


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
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    if hasattr(value, "__dict__"):
        return {
            key: _json(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return value


def _action_status(response: dict[str, Any]) -> str | None:
    result = response.get("result", {})
    if not isinstance(result, dict):
        return None
    outer_status = result.get("status")
    if outer_status and outer_status != "done":
        return str(outer_status)
    nested = result.get("result", {})
    if isinstance(nested, dict) and nested.get("status"):
        return str(nested["status"])
    return str(outer_status) if outer_status else None


def _action_error(response: dict[str, Any]) -> Any:
    result = response.get("result", {})
    if not isinstance(result, dict):
        return response.get("error")
    nested = result.get("result", {})
    if isinstance(nested, dict) and nested.get("error") is not None:
        return nested["error"]
    return result.get("error", response.get("error"))


def _action_error_message(response: dict[str, Any]) -> str | None:
    error = _action_error(response)
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message")
        if code and message:
            return f"{code}: {message}"
        if message:
            return str(message)
        if code:
            return str(code)
    if error is not None:
        return str(error)
    return None


def _contains_error_code(value: Any, code: str) -> bool:
    if isinstance(value, str):
        return code in value
    if isinstance(value, dict):
        return any(_contains_error_code(item, code) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_error_code(item, code) for item in value)
    return False


def _position_xy(entity: Any) -> tuple[float, float] | None:
    data = _json(entity)
    if not isinstance(data, dict):
        return None
    pose = data.get("pose", {})
    position = pose.get("position", []) if isinstance(pose, dict) else []
    if (
        not isinstance(position, list)
        or len(position) < 2
        or not all(isinstance(value, (int, float)) for value in position[:2])
    ):
        return None
    return float(position[0]), float(position[1])


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

    async def _scene_entity(self, entity_id: str) -> dict[str, Any]:
        scene = _json(await self._require_session().state.get("scene_state"))
        entities = scene.get("entities", {})
        for key, value in entities.items():
            entity = _json(value)
            if key == entity_id or entity.get("entity_id") == entity_id:
                return entity
        choices = sorted(
            {str(key) for key in entities}
            | {
                str(_json(value).get("entity_id"))
                for value in entities.values()
                if _json(value).get("entity_id")
            }
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

    @staticmethod
    def _motion_is_stopped(
        state: dict[str, Any], *, require_navigation_inactive: bool = False
    ) -> bool:
        runtime = state.get("runtime", {})
        robot = state.get("robot", {})
        extra = robot.get("extra", {}) if isinstance(robot, dict) else {}
        command = extra.get("command", {}) if isinstance(extra, dict) else {}
        navigation = extra.get("nav", {}) if isinstance(extra, dict) else {}
        velocities = [
            command.get(axis) if isinstance(command, dict) else None
            for axis in ("vx", "vy", "wz")
        ]
        navigation_inactive = (
            isinstance(navigation, dict) and navigation.get("active") is False
            if require_navigation_inactive
            else not (isinstance(navigation, dict) and navigation.get("active") is True)
        )
        return (
            isinstance(runtime, dict)
            and runtime.get("status") == "ready"
            and navigation_inactive
            and all(
                isinstance(value, (int, float)) and abs(float(value)) <= 1e-4
                for value in velocities
            )
        )

    async def _wait_for_motion_stop(
        self,
        timeout_s: float = 5.0,
        *,
        require_navigation_inactive: bool = False,
    ) -> tuple[dict[str, Any] | None, str | None]:
        deadline = time.monotonic() + timeout_s
        last_state: dict[str, Any] | None = None
        last_error: str | None = None
        while True:
            try:
                last_state = await self.robot_state()
                last_error = None
                if self._motion_is_stopped(
                    last_state,
                    require_navigation_inactive=require_navigation_inactive,
                ):
                    return last_state, None
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            if time.monotonic() >= deadline:
                return last_state, last_error
            await asyncio.sleep(0.1)

    async def _cancel_and_confirm_motion_stop(
        self, *, require_navigation_inactive: bool
    ) -> dict[str, Any]:
        stop_response: dict[str, Any] | None = None
        stop_error: str | None = None
        try:
            stop_response = await self.cancel()
        except Exception as exc:
            stop_error = f"{type(exc).__name__}: {exc}"

        final_state, state_error = await self._wait_for_motion_stop(
            require_navigation_inactive=require_navigation_inactive
        )
        stopped = final_state is not None and self._motion_is_stopped(
            final_state,
            require_navigation_inactive=require_navigation_inactive,
        )
        result: dict[str, Any] = {"motion_stopped": stopped}
        if stop_response is not None:
            result["stop_result"] = stop_response
        if stop_error is not None:
            result["stop_error"] = stop_error
        if final_state is not None:
            result["robot_state"] = final_state
        if state_error is not None:
            result["state_warning"] = state_error
        return result

    async def _recover_motion_timeout(
        self,
        action: str,
        exc: TimeoutError,
        *,
        require_navigation_inactive: bool,
    ) -> dict[str, Any]:
        recovery = await self._cancel_and_confirm_motion_stop(
            require_navigation_inactive=require_navigation_inactive
        )
        stopped = recovery["motion_stopped"] is True
        response: dict[str, Any] = {
            "action": action,
            "status": (
                "timed_out_stopped" if stopped else "timed_out_motion_unconfirmed"
            ),
            "error": f"{type(exc).__name__}: {exc}",
            **recovery,
        }
        return response

    async def _invoke_velocity(self, parameters: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self._invoke("set_velocity", parameters, timeout_s=30)
        except TimeoutError as exc:
            return await self._recover_motion_timeout(
                "set_velocity",
                exc,
                require_navigation_inactive=False,
            )

    async def walk(
        self, forward_speed: float, lateral_speed: float, seconds: float
    ) -> dict[str, Any]:
        return await self._invoke_velocity(
            {
                "vx": _number("forward_speed", forward_speed, -1.5, 1.5),
                "vy": _number("lateral_speed", lateral_speed, -1.5, 1.5),
                "wz": 0.0,
                "duration_s": _number("seconds", seconds, 0.1, 10.0),
            }
        )

    async def turn(self, turn_speed: float, seconds: float) -> dict[str, Any]:
        turn_speed = _number("turn_speed", turn_speed, -0.6, 0.6)
        return await self._invoke_velocity(
            {
                "vx": 0.2 if abs(turn_speed) > 0.05 else 0.0,
                "vy": 0.0,
                "wz": turn_speed,
                "duration_s": _number("seconds", seconds, 0.1, 10.0),
            }
        )

    async def go_to(self, entity_id: str) -> dict[str, Any]:
        target = await self._scene_entity(entity_id)
        target_position = _position_xy(target)
        state_before = await self.robot_state()
        robot_before = state_before.get("robot", {})
        previous_position = _position_xy(robot_before)
        previous_distance = (
            math.dist(previous_position, target_position)
            if previous_position is not None and target_position is not None
            else None
        )
        attempt_summaries: list[dict[str, Any]] = []

        for attempt in range(1, 4):
            try:
                response = await self._invoke(
                    "go_to",
                    {"target": {"kind": "entity", "entity_id": entity_id}},
                    timeout_s=300,
                )
            except TimeoutError as exc:
                response = await self._recover_motion_timeout(
                    "go_to",
                    exc,
                    require_navigation_inactive=True,
                )
                attempt_summaries.append(
                    {
                        "attempt": attempt,
                        "status": response["status"],
                        "error": response["error"],
                        "progress_m": None,
                        "retry_safe": False,
                    }
                )
                response["navigation"] = {
                    "status": response["status"],
                    "attempts": attempt,
                    "history": attempt_summaries,
                }
                return response

            status = _action_status(response)
            error = _action_error(response)
            robot_state = response.get("robot_state", {})
            robot_after = (
                robot_state.get("robot", {}) if isinstance(robot_state, dict) else {}
            )
            position_after = _position_xy(robot_after)
            distance_after = (
                math.dist(position_after, target_position)
                if position_after is not None and target_position is not None
                else None
            )
            progress = (
                previous_distance - distance_after
                if previous_distance is not None and distance_after is not None
                else None
            )
            retry_safe = isinstance(robot_state, dict) and self._motion_is_stopped(
                robot_state, require_navigation_inactive=True
            )
            attempt_summaries.append(
                {
                    "attempt": attempt,
                    "status": status,
                    "error": error,
                    "progress_m": progress,
                    "retry_safe": retry_safe,
                }
            )

            if status == "done":
                response["navigation"] = {
                    "status": "done",
                    "attempts": attempt,
                    "history": attempt_summaries,
                }
                return response

            navigation_stuck = _contains_error_code(error, "NAVIGATION_STUCK")
            meaningful_progress = progress is not None and progress >= 0.1
            if (
                not navigation_stuck
                or not meaningful_progress
                or not retry_safe
                or attempt == 3
            ):
                stop_confirmation: dict[str, Any] | None = None
                if not retry_safe:
                    stop_confirmation = await self._cancel_and_confirm_motion_stop(
                        require_navigation_inactive=True
                    )
                response["status"] = (
                    "navigation_stuck" if navigation_stuck else "navigation_failed"
                )
                response["navigation"] = {
                    "status": response["status"],
                    "attempts": attempt,
                    "history": attempt_summaries,
                }
                if stop_confirmation is not None:
                    response["navigation"]["stop_confirmation"] = stop_confirmation
                return response

            previous_position = position_after
            previous_distance = distance_after

        raise AssertionError("unreachable")

    async def cancel(self) -> dict[str, Any]:
        return await self._invoke("cancel", {}, timeout_s=15)

    async def pick(self, entity_id: str) -> dict[str, Any]:
        await self._validate_entity(entity_id, allow_cube_alias=True)
        response = await self._invoke(
            "pick_entity",
            {"target": {"kind": "entity", "entity_id": entity_id}},
            timeout_s=60,
        )
        if _action_status(response) != "done":
            return response

        result = response.get("result", {})
        action_result = result.get("result", {}) if isinstance(result, dict) else {}
        result_held_entity_id = (
            action_result.get("held") if isinstance(action_result, dict) else None
        )
        robot_state = response.get("robot_state", {})
        robot = robot_state.get("robot", {}) if isinstance(robot_state, dict) else {}
        state_held_entity_ids = (
            robot.get("held_entity_ids", []) if isinstance(robot, dict) else []
        )
        if not isinstance(state_held_entity_ids, list):
            state_held_entity_ids = []

        held_entity_id = (
            state_held_entity_ids[0] if state_held_entity_ids else result_held_entity_id
        )
        if not held_entity_id:
            response.update(
                {
                    "status": "unexpected_pick",
                    "requested_entity_id": entity_id,
                    "held_entity_id": None,
                    "message": (
                        "Runtime reported the pick done, but no held entity was "
                        "confirmed."
                    ),
                }
            )
            return response
        if entity_id == "cube":
            return response
        pick_matches_request = (
            entity_id in state_held_entity_ids
            if state_held_entity_ids
            else held_entity_id == entity_id
        )
        if held_entity_id and not pick_matches_request:
            response.update(
                {
                    "status": "unexpected_pick",
                    "requested_entity_id": entity_id,
                    "held_entity_id": held_entity_id,
                    "message": (
                        "Picked up an unexpected item: requested "
                        f"{entity_id}, but the runtime picked {held_entity_id}."
                    ),
                }
            )
        return response

    async def place(
        self, entity_id: str, *, allow_recycle: bool = False
    ) -> dict[str, Any]:
        if not isinstance(allow_recycle, bool):
            raise ValueError("allow_recycle must be a boolean")
        robot_state_before = await self.robot_state()
        robot_before = robot_state_before.get("robot", {})
        held_ids = robot_before.get("held_entity_ids", [])
        if not isinstance(held_ids, list) or len(held_ids) != 1:
            raise ValueError("Robot must be holding exactly one entity before place")
        held_entity_id = held_ids[0]

        target = await self._scene_entity(entity_id)
        target_id = target.get("entity_id", entity_id)
        if target.get("visible") is False:
            raise ValueError(f"Cannot place onto invisible entity {entity_id!r}")
        if target.get("attached_to"):
            raise ValueError(f"Cannot place onto attached entity {entity_id!r}")

        target_state = target.get("state", {})
        if not isinstance(target_state, dict):
            target_state = {}
        target_parent_pad_id = target_state.get("parent_pad_id")
        recycles_source = target_id == "pad_A" or target_parent_pad_id == "pad_A"
        if recycles_source and not allow_recycle:
            raise ValueError(
                f"Placement target {entity_id!r} resolves to source pad_A and would "
                "recycle the held entity. Pass allow_recycle=True only when that is "
                "intentional."
            )

        expected_parent_pad_id = (
            target_id if str(target_id).startswith("pad_") else target_parent_pad_id
        )
        if expected_parent_pad_id is None:
            raise ValueError(
                f"Placement target {entity_id!r} is not a pad or an entity on a pad"
            )

        response = await self._invoke(
            "place_entity",
            {"target": {"kind": "entity", "entity_id": entity_id}},
            timeout_s=60,
        )

        native_status = _action_status(response)
        if native_status != "done":
            response["status"] = "place_failed"
            return response

        scene_after = _json(await self._require_session().state.get("scene_state"))
        entities_after = scene_after.get("entities", {})
        placed_entity: dict[str, Any] | None = None
        for key, value in entities_after.items():
            candidate = _json(value)
            if key == held_entity_id or candidate.get("entity_id") == held_entity_id:
                placed_entity = candidate
                break

        robot_state_after = response.get("robot_state", {})
        robot_after = (
            robot_state_after.get("robot", {})
            if isinstance(robot_state_after, dict)
            else {}
        )
        held_after = (
            robot_after.get("held_entity_ids", [])
            if isinstance(robot_after, dict)
            else []
        )
        placement = {
            "held_entity_id": held_entity_id,
            "target_entity_id": target_id,
            "expected_parent_pad_id": expected_parent_pad_id,
        }
        response["placement"] = placement

        if held_entity_id in held_after:
            placement["status"] = "still_held"
            response["status"] = "unexpected_place"
            response["message"] = (
                f"Runtime reported placement done, but {held_entity_id!r} is still held."
            )
            return response
        if placed_entity is None:
            placement["status"] = "missing"
            response["status"] = "unexpected_place"
            response["message"] = (
                f"Runtime reported placement done, but {held_entity_id!r} is absent "
                "from the refreshed scene."
            )
            return response

        placed_state = placed_entity.get("state", {})
        if not isinstance(placed_state, dict):
            placed_state = {}
        actual_parent_pad_id = placed_state.get("parent_pad_id")
        placement.update(
            {
                "actual_parent_pad_id": actual_parent_pad_id,
                "visible": placed_entity.get("visible"),
            }
        )
        if recycles_source:
            recycled = (
                placed_entity.get("visible") is False and actual_parent_pad_id is None
            )
            placement["status"] = "recycled" if recycled else "unexpected"
            if not recycled:
                response["status"] = "unexpected_place"
                response["message"] = (
                    "Source recycling was requested, but the refreshed scene did not "
                    "show the expected recycled state."
                )
            return response

        verified = (
            placed_entity.get("visible") is not False
            and actual_parent_pad_id == expected_parent_pad_id
        )
        placement["status"] = "verified" if verified else "unexpected"
        if not verified:
            response["status"] = "unexpected_place"
            response["message"] = (
                f"Runtime reported placement done, but {held_entity_id!r} is on "
                f"{actual_parent_pad_id!r}, expected {expected_parent_pad_id!r}."
            )
        return response

    async def execute_code(self, code: str) -> dict[str, Any]:
        """Run a bounded executable plan over guarded controller operations."""
        executor = MenloCodeExecutor(self._execute_code_call)
        return await executor.execute(code)

    async def _execute_code_call(
        self, method: str, arguments: list[Any], keywords: dict[str, Any]
    ) -> Any:
        operations = {
            "get_robot_state": self.robot_state,
            "get_scene": self.scene,
            "go_to": self.go_to,
            "pick": self.pick,
            "place": self.place,
            "stop": self.cancel,
            "turn": self.turn,
            "walk": self.walk,
        }
        operation = operations[method]
        result = await operation(*arguments, **keywords)
        if method in {"get_robot_state", "get_scene"}:
            return result

        failure: str | None = None
        if method == "go_to":
            navigation = result.get("navigation", {})
            if navigation.get("status") != "done":
                failure = _action_error_message(result) or (
                    f"navigation status is {navigation.get('status')!r}"
                )
        elif method == "pick":
            if result.get("status") == "unexpected_pick":
                failure = result.get("message", "picked an unexpected entity")
            elif _action_status(result) != "done":
                failure = _action_error_message(result) or (
                    f"pick status is {_action_status(result)!r}"
                )
        elif method == "place":
            placement = result.get("placement", {})
            if placement.get("status") not in {"verified", "recycled"}:
                failure = (
                    result.get("message")
                    or _action_error_message(result)
                    or f"placement status is {placement.get('status')!r}"
                )
        elif method in {"walk", "turn"}:
            status = result.get("status")
            if isinstance(status, str) and status.startswith("timed_out_"):
                failure = f"motion status is {status!r}"
            elif _action_status(result) != "done":
                failure = _action_error_message(result) or (
                    f"motion status is {_action_status(result)!r}"
                )
        elif method == "stop":
            if _action_status(result) != "done":
                failure = _action_error_message(result) or (
                    f"stop status is {_action_status(result)!r}"
                )
            else:
                final_state, state_error = await self._wait_for_motion_stop(
                    require_navigation_inactive=True
                )
                stopped = final_state is not None and self._motion_is_stopped(
                    final_state, require_navigation_inactive=True
                )
                result["stop_confirmation"] = {
                    "motion_stopped": stopped,
                    "robot_state": final_state,
                }
                if state_error is not None:
                    result["stop_confirmation"]["state_warning"] = state_error
                if not stopped:
                    failure = "motion did not reach a confirmed stopped state"

        if failure is not None:
            raise PlanActionError(method, failure, result)
        return result

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
                _number(
                    "pitch_degrees",
                    pitch_degrees,
                    HEAD_PITCH_MIN_DEGREES,
                    HEAD_PITCH_MAX_DEGREES,
                )
            )
        return await self._invoke("set_head", parameters, timeout_s=15)

    async def look(
        self, yaw_degrees: float | None, pitch_degrees: float | None
    ) -> bytes:
        requested: dict[str, float] = {}
        if yaw_degrees is not None:
            requested["yaw"] = math.radians(
                _number("yaw_degrees", yaw_degrees, -80, 80)
            )
        if pitch_degrees is not None:
            requested["pitch"] = math.radians(
                _number(
                    "pitch_degrees",
                    pitch_degrees,
                    HEAD_PITCH_MIN_DEGREES,
                    HEAD_PITCH_MAX_DEGREES,
                )
            )
        if requested:
            head_response = await self.aim_head(yaw_degrees, pitch_degrees)
            if _action_status(head_response) != "done":
                error = _action_error(head_response)
                raise RuntimeError(f"Head aim failed: {error!r}")
            deadline = time.monotonic() + 3.0
            tolerance = math.radians(2.0)
            while True:
                state = await self.robot_state()
                robot = state.get("robot", {})
                extra = robot.get("extra", {}) if isinstance(robot, dict) else {}
                head = extra.get("head", {}) if isinstance(extra, dict) else {}
                measured = head.get("measured", {}) if isinstance(head, dict) else {}
                if not isinstance(measured, dict):
                    measured = {}
                converged = all(
                    isinstance(measured.get(axis), (int, float))
                    and abs(float(measured[axis]) - target) <= tolerance
                    for axis, target in requested.items()
                )
                if converged:
                    break
                if time.monotonic() >= deadline:
                    target_degrees = {
                        axis: round(math.degrees(value), 1)
                        for axis, value in requested.items()
                    }
                    measured_degrees = {
                        axis: round(math.degrees(value), 1)
                        for axis, value in measured.items()
                        if isinstance(value, (int, float)) and math.isfinite(value)
                    }
                    raise TimeoutError(
                        "Head did not converge before capture within 3.0s "
                        f"(target_degrees={target_degrees}, "
                        f"measured_degrees={measured_degrees})"
                    )
                await asyncio.sleep(0.1)
        return await self.camera()


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
async def look(
    yaw_degrees: float | None = None,
    pitch_degrees: float | None = None,
) -> Image:
    """Optionally aim and capture JPEG; SimpleSim pitch is -40° up to +20° down."""
    return Image(data=await controller.look(yaw_degrees, pitch_degrees), format="jpeg")


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
async def place(entity_id: str, allow_recycle: bool = False) -> dict[str, Any]:
    """Place and verify an object; source recycling requires explicit opt-in."""
    return await controller.place(entity_id, allow_recycle=allow_recycle)


@mcp.tool()
async def menlo_execute(code: str) -> dict[str, Any]:
    """Execute a high-confidence robot plan in a restricted Python subset.

    Write synchronous-looking calls such as ``menlo.go_to("pad_B")``; do not use
    ``await``. Allowed methods are get_robot_state, get_scene, go_to, pick, place,
    stop, turn, and walk. Assignments, if/for, return, assert, comparisons, indexing,
    and basic pure builtins are supported. Imports, functions, arbitrary attributes,
    filesystem/network access, lifecycle calls, and camera capture are prohibited.
    The plan is validated before motion, bounded by operation/loop/time budgets, and
    stops immediately when a guarded action fails its postcondition.
    """
    return await controller.execute_code(code)


if __name__ == "__main__":
    mcp.run(transport="stdio")
