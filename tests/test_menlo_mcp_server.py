from __future__ import annotations

import math
import unittest
from types import SimpleNamespace

from menlo_mcp_server import MenloRobotController, _number


class FakeState:
    def __init__(self) -> None:
        self.robot_status: dict = {"robot": {"status": "ready"}}

    async def get(self, name: str):
        if name == "scene_state":
            entity = SimpleNamespace(entity_id="cube_4")
            return SimpleNamespace(entities={"cube_4": entity})
        return self.robot_status


class FakeSession:
    def __init__(self) -> None:
        self.state = FakeState()
        self.calls: list[tuple[str, dict, float | None]] = []
        self.invoke_result: dict = {"status": "done"}

    async def discover_skills(self):
        return [
            SimpleNamespace(name=name)
            for name in (
                "set_velocity",
                "go_to",
                "cancel",
                "pick_entity",
                "place_entity",
                "set_head",
            )
        ]

    async def invoke(self, skill, parameters, *, timeout_s=None):
        self.calls.append((skill, parameters, timeout_s))
        return self.invoke_result


class FakeRobots:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.deleted: list[str] = []

    async def delete(self, robot_id: str) -> None:
        self.deleted.append(robot_id)
        if self.error:
            raise self.error


class FakeClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.robots = FakeRobots(error)
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.controller = MenloRobotController()
        self.session = FakeSession()
        self.controller.session = self.session  # type: ignore[assignment]

    async def test_walk_maps_intuitive_speeds_to_menlo_velocity(self):
        await self.controller.walk(-0.5, 0.25, 2.0)
        self.assertEqual(
            self.session.calls[-1],
            (
                "set_velocity",
                {"vx": -0.5, "vy": 0.25, "wz": 0.0, "duration_s": 2.0},
                30,
            ),
        )

    async def test_turn_adds_a_small_forward_step(self):
        await self.controller.turn(-0.4, 1.5)
        _, parameters, _ = self.session.calls[-1]
        self.assertEqual(parameters["vx"], 0.2)
        self.assertEqual(parameters["wz"], -0.4)

    async def test_go_to_uses_native_entity_target(self):
        await self.controller.go_to("cube_4")
        self.assertEqual(
            self.session.calls[-1],
            (
                "go_to",
                {"target": {"kind": "entity", "entity_id": "cube_4"}},
                300,
            ),
        )

    async def test_aim_head_converts_degrees_to_radians(self):
        await self.controller.aim_head(45, -20)
        _, parameters, _ = self.session.calls[-1]
        self.assertAlmostEqual(parameters["yaw"], math.pi / 4)
        self.assertAlmostEqual(parameters["pitch"], math.radians(-20))

    async def test_pick_place_and_stop_map_to_native_skills(self):
        await self.controller.pick("cube")
        self.assertEqual(
            self.session.calls[-1][0:2],
            (
                "pick_entity",
                {"target": {"kind": "entity", "entity_id": "cube"}},
            ),
        )

        await self.controller.place("cube_4")
        self.assertEqual(
            self.session.calls[-1][0:2],
            (
                "place_entity",
                {"target": {"kind": "entity", "entity_id": "cube_4"}},
            ),
        )

        await self.controller.cancel()
        self.assertEqual(self.session.calls[-1][0:2], ("cancel", {}))

    async def test_exact_pick_reports_unexpected_held_entity(self):
        self.session.invoke_result = {
            "status": "done",
            "result": {"status": "done", "held": "cube_0", "held_count": 1},
        }

        response = await self.controller.pick("cube_4")

        self.assertEqual(
            self.session.calls[-1][0:2],
            (
                "pick_entity",
                {"target": {"kind": "entity", "entity_id": "cube_4"}},
            ),
        )
        self.assertEqual(response["status"], "unexpected_pick")
        self.assertEqual(response["requested_entity_id"], "cube_4")
        self.assertEqual(response["held_entity_id"], "cube_0")
        self.assertIn("requested cube_4", response["message"])

    async def test_exact_pick_accepts_requested_held_entity(self):
        self.session.invoke_result = {
            "status": "done",
            "result": {"status": "done", "held": "cube_4", "held_count": 1},
        }

        response = await self.controller.pick("cube_4")

        self.assertNotIn("status", response)
        self.assertNotIn("requested_entity_id", response)

    async def test_exact_pick_prefers_final_robot_state(self):
        self.session.invoke_result = {
            "status": "done",
            "result": {"status": "done", "held": "cube_4", "held_count": 1},
        }
        self.session.state.robot_status = {
            "robot": {"status": "holding", "held_entity_ids": ["cube_0"]}
        }

        response = await self.controller.pick("cube_4")

        self.assertEqual(response["status"], "unexpected_pick")
        self.assertEqual(response["held_entity_id"], "cube_0")

    async def test_cube_alias_accepts_any_held_cube(self):
        self.session.invoke_result = {
            "status": "done",
            "result": {"status": "done", "held": "cube_0", "held_count": 1},
        }

        response = await self.controller.pick("cube")

        self.assertNotIn("status", response)
        self.assertNotIn("requested_entity_id", response)

    async def test_aim_head_requires_at_least_one_angle(self):
        with self.assertRaisesRegex(ValueError, "Provide yaw_degrees"):
            await self.controller.aim_head(None, None)

    async def test_unknown_entity_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown entity_id"):
            await self.controller.go_to("not-real")

    def test_number_rejects_out_of_range_instead_of_silently_clamping(self):
        with self.assertRaisesRegex(ValueError, "between"):
            _number("speed", 2.0, -1.5, 1.5)

    async def test_shutdown_reports_cleanup_failure(self):
        client = FakeClient(RuntimeError("delete failed"))
        controller = MenloRobotController()
        controller.client = client  # type: ignore[assignment]
        controller.robot_id = "robot-1"

        result = await controller.shutdown()

        self.assertEqual(result["status"], "stopped_with_warnings")
        self.assertIn("delete failed", result["cleanup_warnings"][0])
        self.assertTrue(client.closed)


if __name__ == "__main__":
    unittest.main()
