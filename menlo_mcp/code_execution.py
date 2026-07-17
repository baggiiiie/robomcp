"""Connect bounded Menlo code plans to guarded controller operations."""

from __future__ import annotations

from typing import Any, Protocol

from menlo_code_mode import MenloCodeExecutor, PlanActionError

from .models import action_error_message, action_payload


class ControllerOperations(Protocol):
    async def robot_state(self) -> dict[str, Any]: ...
    async def scene(self) -> dict[str, Any]: ...
    async def go_to(self, entity_id: str) -> dict[str, Any]: ...
    async def pick(self, entity_id: str) -> dict[str, Any]: ...
    async def place(
        self, entity_id: str, *, allow_recycle: bool = False
    ) -> dict[str, Any]: ...
    async def cancel(self) -> dict[str, Any]: ...
    async def turn(self, turn_speed: float, seconds: float) -> dict[str, Any]: ...
    async def walk(
        self, forward_speed: float, lateral_speed: float, seconds: float
    ) -> dict[str, Any]: ...
    async def _wait_for_motion_stop(
        self, timeout_s: float = 5.0, *, require_navigation_inactive: bool = False
    ) -> tuple[dict[str, Any] | None, str | None]: ...
    def _motion_is_stopped(
        self,
        state: dict[str, Any],
        *,
        require_navigation_inactive: bool = False,
    ) -> bool: ...


class CodeExecutionMixin:
    async def execute_code(self, code: str) -> dict[str, Any]:
        return await MenloCodeExecutor(self._execute_code_call).execute(code)

    async def _execute_code_call(
        self: ControllerOperations,
        method: str,
        arguments: list[Any],
        keywords: dict[str, Any],
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
        result = await operations[method](*arguments, **keywords)
        if method in {"get_robot_state", "get_scene"}:
            return result

        failure = None
        action = action_payload(result)
        if method == "go_to" and result.get("navigation", {}).get("status") != "done":
            failure = action_error_message(result) or "navigation failed"
        elif method == "pick" and result.get("status") == "unexpected_pick":
            failure = result["message"]
        elif method == "pick" and action.effective_status != "done":
            failure = action_error_message(result) or "pick failed"
        elif method == "place" and result.get("placement", {}).get("status") not in {
            "benchmark_consumed",
            "verified",
            "recycled",
        }:
            failure = (
                result.get("message") or action_error_message(result) or "place failed"
            )
        elif method in {"walk", "turn"}:
            if str(result.get("status", "")).startswith("timed_out_"):
                failure = f"motion status is {result['status']!r}"
            elif action.effective_status != "done":
                failure = action_error_message(result) or "motion failed"
        elif method == "stop":
            if action.effective_status != "done":
                failure = action_error_message(result) or "stop failed"
            else:
                state, warning = await self._wait_for_motion_stop(
                    require_navigation_inactive=True
                )
                stopped = bool(
                    state
                    and self._motion_is_stopped(state, require_navigation_inactive=True)
                )
                result["stop_confirmation"] = {
                    "motion_stopped": stopped,
                    "robot_state": state,
                    **({"state_warning": warning} if warning else {}),
                }
                if not stopped:
                    failure = "motion did not reach a confirmed stopped state"

        if failure:
            raise PlanActionError(method, failure, result)
        return result
