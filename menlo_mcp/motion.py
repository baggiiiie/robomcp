"""Recover persistent timed-out motion and safely retry partial stuck navigation."""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any

from .connection import MenloConnection
from .models import RobotState, action_payload, number


class MotionMixin(MenloConnection):
    @staticmethod
    def _motion_is_stopped(
        state: dict[str, Any], *, require_navigation_inactive: bool = False
    ) -> bool:
        return RobotState.model_validate(state).motion_is_stopped(
            require_navigation_inactive=require_navigation_inactive
        )

    async def _wait_for_motion_stop(
        self,
        timeout_s: float = 5.0,
        *,
        require_navigation_inactive: bool = False,
    ) -> tuple[dict[str, Any] | None, str | None]:
        deadline = time.monotonic() + timeout_s
        last_state, last_error = None, None
        while time.monotonic() < deadline:
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
            await asyncio.sleep(0.1)
        return last_state, last_error

    async def _cancel_and_confirm_motion_stop(
        self, *, require_navigation_inactive: bool
    ) -> dict[str, Any]:
        try:
            stop_result, stop_error = await self.cancel(), None
        except Exception as exc:
            stop_result, stop_error = None, f"{type(exc).__name__}: {exc}"

        state, state_error = await self._wait_for_motion_stop(
            require_navigation_inactive=require_navigation_inactive
        )
        stopped = bool(
            state
            and self._motion_is_stopped(
                state, require_navigation_inactive=require_navigation_inactive
            )
        )
        return {
            "motion_stopped": stopped,
            **({"stop_result": stop_result} if stop_result else {}),
            **({"stop_error": stop_error} if stop_error else {}),
            **({"robot_state": state} if state else {}),
            **({"state_warning": state_error} if state_error else {}),
        }

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
        return {
            "action": action,
            "status": (
                "timed_out_stopped"
                if recovery["motion_stopped"]
                else "timed_out_motion_unconfirmed"
            ),
            "error": f"{type(exc).__name__}: {exc}",
            **recovery,
        }

    async def _invoke_velocity(self, parameters: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self._invoke("set_velocity", parameters, timeout_s=30)
        except TimeoutError as exc:
            return await self._recover_motion_timeout(
                "set_velocity", exc, require_navigation_inactive=False
            )

    async def walk(
        self, forward_speed: float, lateral_speed: float, seconds: float
    ) -> dict[str, Any]:
        return await self._invoke_velocity(
            {
                "vx": number("forward_speed", forward_speed, -1.5, 1.5),
                "vy": number("lateral_speed", lateral_speed, -1.5, 1.5),
                "wz": 0.0,
                "duration_s": number("seconds", seconds, 0.1, 10.0),
            }
        )

    async def turn(self, turn_speed: float, seconds: float) -> dict[str, Any]:
        speed = number("turn_speed", turn_speed, -0.6, 0.6)
        return await self._invoke_velocity(
            {
                "vx": 0.2 if abs(speed) > 0.05 else 0.0,
                "vy": 0.0,
                "wz": speed,
                "duration_s": number("seconds", seconds, 0.1, 10.0),
            }
        )

    async def go_to(self, entity_id: str) -> dict[str, Any]:
        target = (await self._scene_model()).find(entity_id)
        target_xy = target.pose.xy
        previous_xy = (await self._robot_state_model()).robot.pose.xy
        previous_distance = (
            math.dist(previous_xy, target_xy) if previous_xy and target_xy else None
        )
        history = []

        for attempt in range(1, 4):
            try:
                response = await self._invoke(
                    "go_to",
                    {"target": {"kind": "entity", "entity_id": entity_id}},
                    timeout_s=300,
                )
            except TimeoutError as exc:
                response = await self._recover_motion_timeout(
                    "go_to", exc, require_navigation_inactive=True
                )
                history.append(
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
                    "history": history,
                }
                return response

            action = action_payload(response)
            state = RobotState.model_validate(response.get("robot_state", {}))
            position = state.robot.pose.xy
            distance = (
                math.dist(position, target_xy) if position and target_xy else None
            )
            progress = (
                previous_distance - distance
                if previous_distance is not None and distance is not None
                else None
            )
            retry_safe = state.motion_is_stopped(require_navigation_inactive=True)
            history.append(
                {
                    "attempt": attempt,
                    "status": action.effective_status,
                    "error": action.effective_error,
                    "progress_m": progress,
                    "retry_safe": retry_safe,
                }
            )

            if action.effective_status == "done":
                response["navigation"] = {
                    "status": "done",
                    "attempts": attempt,
                    "history": history,
                }
                return response

            stuck = action.error_code == "NAVIGATION_STUCK"
            should_retry = (
                stuck and progress is not None and progress >= 0.1 and retry_safe
            )
            if not should_retry or attempt == 3:
                response["status"] = (
                    "navigation_stuck" if stuck else "navigation_failed"
                )
                navigation: dict[str, Any] = {
                    "status": response["status"],
                    "attempts": attempt,
                    "history": history,
                }
                response["navigation"] = navigation
                if not retry_safe:
                    navigation["stop_confirmation"] = (
                        await self._cancel_and_confirm_motion_stop(
                            require_navigation_inactive=True
                        )
                    )
                return response

            previous_distance = distance

        raise AssertionError("unreachable")

    async def cancel(self) -> dict[str, Any]:
        return await self._invoke("cancel", {}, timeout_s=15)
