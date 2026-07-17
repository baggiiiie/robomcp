"""Detect wrong-object picks, source recycling, and unverified placements."""

from __future__ import annotations

from typing import Any

from .connection import MenloConnection
from .models import RobotState, action_payload


def _benchmark_consumed_on_expected_pad(
    before: RobotState, after: RobotState, expected_pad: str
) -> bool:
    """Recognize benchmark placements that consume the original source entity."""

    before_benchmark = before.robot.extra.model_dump(mode="python").get(
        "sort_benchmark"
    )
    after_benchmark = after.robot.extra.model_dump(mode="python").get("sort_benchmark")
    if not isinstance(before_benchmark, dict) or not isinstance(after_benchmark, dict):
        return False
    if before_benchmark.get("enabled") is not True:
        return False
    if after_benchmark.get("enabled") is not True:
        return False

    before_stacks = before_benchmark.get("pallet_stack_counts")
    after_stacks = after_benchmark.get("pallet_stack_counts")
    if not isinstance(before_stacks, dict) or not isinstance(after_stacks, dict):
        return False

    counters = ("correct_count", "wrong_count", "spoiled_count")
    if any(
        type(before_benchmark.get(counter)) is not int
        or type(after_benchmark.get(counter)) is not int
        for counter in counters
    ):
        return False
    if type(before_stacks.get(expected_pad)) is not int:
        return False
    if type(after_stacks.get(expected_pad)) is not int:
        return False

    return (
        after_benchmark["correct_count"] == before_benchmark["correct_count"] + 1
        and after_benchmark["wrong_count"] == before_benchmark["wrong_count"]
        and after_benchmark["spoiled_count"] == before_benchmark["spoiled_count"]
        and after_stacks[expected_pad] == before_stacks[expected_pad] + 1
    )


class ManipulationMixin(MenloConnection):
    async def pick(self, entity_id: str) -> dict[str, Any]:
        if not entity_id.strip():
            raise ValueError("entity_id must not be empty")
        if entity_id != "cube":
            (await self._scene_model()).find(entity_id)

        response = await self._invoke(
            "pick_entity",
            {"target": {"kind": "entity", "entity_id": entity_id}},
            timeout_s=60,
        )
        action = action_payload(response)
        if action.effective_status != "done":
            return response

        reported_held = (action.result or {}).get("held")
        held_ids = RobotState.model_validate(
            response.get("robot_state", {})
        ).robot.held_entity_ids
        held = held_ids[0] if held_ids else reported_held
        if held and (entity_id == "cube" or held == entity_id):
            return response

        response.update(
            {
                "status": "unexpected_pick",
                "requested_entity_id": entity_id,
                "held_entity_id": held,
                "message": (
                    f"Picked up an unexpected item: requested {entity_id}, but the "
                    f"runtime picked {held}."
                    if held
                    else (
                        "Runtime reported the pick done, but no held entity was "
                        "confirmed."
                    )
                ),
            }
        )
        return response

    async def place(
        self, entity_id: str, *, allow_recycle: bool = False
    ) -> dict[str, Any]:
        if type(allow_recycle) is not bool:
            raise ValueError("allow_recycle must be a boolean")

        state_before = await self._robot_state_model()
        held_ids = state_before.robot.held_entity_ids
        if len(held_ids) != 1:
            raise ValueError("Robot must be holding exactly one entity before place")
        held_id = held_ids[0]

        target = (await self._scene_model()).find(entity_id)
        if not target.visible:
            raise ValueError(f"Cannot place onto invisible entity {entity_id!r}")
        if target.attached_to:
            raise ValueError(f"Cannot place onto attached entity {entity_id!r}")

        parent_pad = target.state.parent_pad_id
        recycles_source = target.entity_id == "pad_A" or parent_pad == "pad_A"
        if recycles_source and not allow_recycle:
            raise ValueError(
                f"Placement target {entity_id!r} resolves to source pad_A and would "
                "recycle the held entity. Pass allow_recycle=True only when "
                "intentional."
            )
        expected_pad = (
            target.entity_id if target.entity_id.startswith("pad_") else parent_pad
        )
        if expected_pad is None:
            raise ValueError(
                f"Placement target {entity_id!r} is not a pad or an entity on a pad"
            )

        response = await self._invoke(
            "place_entity",
            {"target": {"kind": "entity", "entity_id": entity_id}},
            timeout_s=60,
        )
        if action_payload(response).effective_status != "done":
            response["status"] = "place_failed"
            return response

        scene_after = await self._scene_model()
        placed = next(
            (
                item
                for key, item in scene_after.entities.items()
                if key == held_id or item.entity_id == held_id
            ),
            None,
        )
        state_after = RobotState.model_validate(response.get("robot_state", {}))
        held_after = state_after.robot.held_entity_ids
        benchmark_consumed = (
            not recycles_source
            and held_id not in held_after
            and _benchmark_consumed_on_expected_pad(
                state_before, state_after, expected_pad
            )
        )
        placement = {
            "held_entity_id": held_id,
            "target_entity_id": target.entity_id,
            "expected_parent_pad_id": expected_pad,
        }
        response["placement"] = placement

        if held_id in held_after:
            placement["status"] = "still_held"
            response["status"] = "unexpected_place"
            response["message"] = (
                f"Runtime reported placement done, but {held_id!r} is still held."
            )
            return response
        if placed is None:
            placement.update(
                {
                    "actual_parent_pad_id": None,
                    "visible": False,
                    "status": (
                        "benchmark_consumed" if benchmark_consumed else "missing"
                    ),
                }
            )
            if not benchmark_consumed:
                response["status"] = "unexpected_place"
                response["message"] = (
                    f"Runtime reported placement done, but {held_id!r} is absent "
                    "from the scene."
                )
            return response

        actual_pad = placed.state.parent_pad_id
        placement.update(
            {"actual_parent_pad_id": actual_pad, "visible": placed.visible}
        )
        verified = (
            not placed.visible and actual_pad is None
            if recycles_source
            else placed.visible and actual_pad == expected_pad
        )
        if (
            not verified
            and benchmark_consumed
            and not placed.visible
            and actual_pad is None
        ):
            verified = True
            placement["status"] = "benchmark_consumed"
        elif not verified:
            placement["status"] = "unexpected"
        else:
            placement["status"] = "recycled" if recycles_source else "verified"
        if not verified:
            response["status"] = "unexpected_place"
            response["message"] = (
                f"Runtime reported placement done, but {held_id!r} is on "
                f"{actual_pad!r}, expected {expected_pad!r}."
            )
        return response
