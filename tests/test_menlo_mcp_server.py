from __future__ import annotations

import math
import unittest
from types import SimpleNamespace

from menlo_mcp_server import MenloRobotController, _number


def _motion_state(
    x: float,
    *,
    runtime_status: str = "ready",
    navigation_active: bool = False,
    vx: float = 0.0,
) -> dict:
    return {
        "runtime": {"status": runtime_status},
        "robot": {
            "status": "busy" if runtime_status == "busy" else "ready",
            "pose": {"position": [x, 0.0, 0.6]},
            "extra": {
                "command": {"vx": vx, "vy": 0.0, "wz": 0.0},
                "nav": {"active": navigation_active},
            },
        },
    }


class FakeState:
    def __init__(self) -> None:
        self.robot_status: dict = {"robot": {"status": "ready"}}
        self.scene_entities: dict = {
            "cube_4": SimpleNamespace(
                entity_id="cube_4",
                visible=True,
                pose=SimpleNamespace(position=[2.0, 0.0, 0.65]),
                state=SimpleNamespace(color="red", parent_pad_id="pad_A"),
            ),
            "pad_A": SimpleNamespace(entity_id="pad_A", visible=True),
            "pad_B": SimpleNamespace(entity_id="pad_B", visible=True),
        }

    async def get(self, name: str):
        if name == "scene_state":
            return SimpleNamespace(entities=self.scene_entities)
        return self.robot_status


class FakeSession:
    def __init__(self) -> None:
        self.state = FakeState()
        self.calls: list[tuple[str, dict, float | None]] = []
        self.invoke_result: dict = {"status": "done"}
        self.invoke_results: dict[str, list[dict]] = {}
        self.invoke_errors: dict[str, list[Exception]] = {}
        self.robot_states_after_invoke: dict[str, list[dict]] = {}
        self.vision_result = b"jpeg"

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
        errors = self.invoke_errors.get(skill, [])
        if errors:
            raise errors.pop(0)
        states = self.robot_states_after_invoke.get(skill, [])
        if states:
            self.state.robot_status = states.pop(0)
        if skill == "place_entity":
            self.state.robot_status = {
                "robot": {"status": "ready", "held_entity_ids": []}
            }
        results = self.invoke_results.get(skill, [])
        return results.pop(0) if results else self.invoke_result

    async def get_vision(self, stream):
        self.calls.append(("get_vision", {"stream": stream}, None))
        return self.vision_result


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

    async def test_velocity_timeout_cancels_and_confirms_zero_velocity(self):
        self.session.state.robot_status = _motion_state(
            0.0, runtime_status="busy", vx=0.5
        )
        self.session.invoke_errors["set_velocity"] = [
            TimeoutError("velocity action timed out")
        ]
        self.session.robot_states_after_invoke["cancel"] = [_motion_state(0.0)]

        response = await self.controller.walk(0.5, 0.0, 1.0)

        self.assertEqual(response["status"], "timed_out_stopped")
        self.assertTrue(response["motion_stopped"])
        self.assertEqual(
            [call[0] for call in self.session.calls],
            ["set_velocity", "cancel"],
        )

    async def test_velocity_timeout_reports_unconfirmed_motion(self):
        self.session.state.robot_status = _motion_state(
            0.0, runtime_status="busy", vx=0.5
        )
        self.session.invoke_errors["set_velocity"] = [
            TimeoutError("velocity action timed out")
        ]

        async def unconfirmed_stop(*args, **kwargs):
            return _motion_state(0.0, runtime_status="busy", vx=0.5), None

        self.controller._wait_for_motion_stop = unconfirmed_stop  # type: ignore[method-assign]

        response = await self.controller.walk(0.5, 0.0, 1.0)

        self.assertEqual(response["status"], "timed_out_motion_unconfirmed")
        self.assertFalse(response["motion_stopped"])

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

    async def test_go_to_retries_stuck_navigation_after_meaningful_progress(self):
        self.session.state.robot_status = _motion_state(0.0)
        self.session.invoke_results["go_to"] = [
            {
                "status": "failed",
                "result": {
                    "status": "failed",
                    "error": {"code": "NAVIGATION_STUCK"},
                },
            },
            {"status": "done", "result": {"status": "done"}},
        ]
        self.session.robot_states_after_invoke["go_to"] = [
            _motion_state(0.5),
            _motion_state(1.2),
        ]

        response = await self.controller.go_to("cube_4")

        self.assertEqual(response["navigation"]["status"], "done")
        self.assertEqual(response["navigation"]["attempts"], 2)
        self.assertEqual(
            [call[0] for call in self.session.calls],
            ["go_to", "go_to"],
        )

    async def test_go_to_does_not_retry_stuck_navigation_without_progress(self):
        self.session.state.robot_status = _motion_state(0.0)
        self.session.invoke_results["go_to"] = [
            {
                "status": "failed",
                "result": {
                    "status": "failed",
                    "error": {"code": "NAVIGATION_STUCK"},
                },
            }
        ]
        self.session.robot_states_after_invoke["go_to"] = [_motion_state(0.02)]

        response = await self.controller.go_to("cube_4")

        self.assertEqual(response["status"], "navigation_stuck")
        self.assertEqual(response["navigation"]["attempts"], 1)
        self.assertEqual(
            [call[0] for call in self.session.calls],
            ["go_to"],
        )

    async def test_go_to_outer_failure_is_not_masked_by_nested_done(self):
        self.session.state.robot_status = _motion_state(0.0)
        self.session.invoke_results["go_to"] = [
            {"status": "failed", "result": {"status": "done"}}
        ]
        self.session.robot_states_after_invoke["go_to"] = [_motion_state(0.2)]

        response = await self.controller.go_to("cube_4")

        self.assertEqual(response["status"], "navigation_failed")
        self.assertEqual(response["navigation"]["attempts"], 1)

    async def test_go_to_timeout_cancels_and_confirms_navigation_stopped(self):
        self.session.state.robot_status = _motion_state(0.0)
        self.session.invoke_errors["go_to"] = [
            TimeoutError("navigation action timed out")
        ]
        self.session.robot_states_after_invoke["cancel"] = [_motion_state(0.3)]

        response = await self.controller.go_to("cube_4")

        self.assertEqual(response["status"], "timed_out_stopped")
        self.assertTrue(response["motion_stopped"])
        self.assertEqual(
            [call[0] for call in self.session.calls],
            ["go_to", "cancel"],
        )

    async def test_go_to_does_not_retry_while_navigation_is_active(self):
        self.session.state.robot_status = _motion_state(0.0)
        self.session.invoke_results["go_to"] = [
            {
                "status": "failed",
                "result": {
                    "status": "failed",
                    "error": {"code": "NAVIGATION_STUCK"},
                },
            }
        ]
        self.session.robot_states_after_invoke["go_to"] = [
            _motion_state(
                0.5,
                runtime_status="busy",
                navigation_active=True,
                vx=0.2,
            )
        ]
        self.session.robot_states_after_invoke["cancel"] = [_motion_state(0.5)]

        response = await self.controller.go_to("cube_4")

        self.assertEqual(response["status"], "navigation_stuck")
        self.assertFalse(response["navigation"]["history"][0]["retry_safe"])
        self.assertTrue(response["navigation"]["stop_confirmation"]["motion_stopped"])
        self.assertEqual(
            [call[0] for call in self.session.calls],
            ["go_to", "cancel"],
        )

    async def test_look_aims_waits_for_convergence_and_captures(self):
        self.session.state.robot_status = {
            "robot": {
                "status": "ready",
                "extra": {
                    "head": {
                        "measured": {
                            "yaw": math.radians(45),
                            "pitch": math.radians(-20),
                        }
                    }
                },
            }
        }

        image = await self.controller.look(45, -20)

        _, parameters, _ = self.session.calls[-2]
        self.assertAlmostEqual(parameters["yaw"], math.pi / 4)
        self.assertAlmostEqual(parameters["pitch"], math.radians(-20))
        self.assertEqual(self.session.calls[-1][0], "get_vision")
        self.assertEqual(image, b"jpeg")

    async def test_look_rejects_unreachable_downward_pitch(self):
        with self.assertRaisesRegex(ValueError, "between -40.0 and 20.0"):
            await self.controller.look(-65, 25)

        self.assertEqual(self.session.calls, [])

    async def test_look_accepts_reachable_downward_pitch_limit(self):
        self.session.state.robot_status = {
            "robot": {
                "status": "ready",
                "extra": {"head": {"measured": {"pitch": math.radians(20)}}},
            }
        }

        image = await self.controller.look(None, 20)

        _, parameters, _ = self.session.calls[-2]
        self.assertAlmostEqual(parameters["pitch"], math.radians(20))
        self.assertEqual(self.session.calls[-1][0], "get_vision")
        self.assertEqual(image, b"jpeg")

    async def test_look_without_angles_only_captures(self):
        image = await self.controller.look(None, None)

        self.assertEqual(
            self.session.calls,
            [("get_vision", {"stream": "pov"}, None)],
        )
        self.assertEqual(image, b"jpeg")

    async def test_look_does_not_capture_after_head_failure(self):
        self.session.invoke_results["set_head"] = [
            {"status": "failed", "error": {"code": "HEAD_BLOCKED"}}
        ]

        with self.assertRaisesRegex(RuntimeError, "Head aim failed"):
            await self.controller.look(45, -20)

        self.assertFalse(any(call[0] == "get_vision" for call in self.session.calls))

    async def test_pick_place_and_stop_map_to_native_skills(self):
        await self.controller.pick("cube")
        self.assertEqual(
            self.session.calls[-1][0:2],
            (
                "pick_entity",
                {"target": {"kind": "entity", "entity_id": "cube"}},
            ),
        )

        self.session.state.robot_status = {
            "robot": {"status": "holding", "held_entity_ids": ["cube_4"]}
        }
        self.session.state.scene_entities["cube_4"].state.parent_pad_id = "pad_B"
        place_response = await self.controller.place("pad_B")
        self.assertEqual(
            self.session.calls[-1][0:2],
            (
                "place_entity",
                {"target": {"kind": "entity", "entity_id": "pad_B"}},
            ),
        )
        self.assertEqual(place_response["placement"]["status"], "verified")

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

    async def test_pick_reports_done_without_a_held_entity(self):
        self.session.invoke_result = {"status": "done", "result": {"status": "done"}}

        response = await self.controller.pick("cube_4")

        self.assertEqual(response["status"], "unexpected_pick")
        self.assertIsNone(response["held_entity_id"])

    async def test_place_rejects_source_recycling_without_opt_in(self):
        self.session.state.robot_status = {
            "robot": {"status": "holding", "held_entity_ids": ["cube_4"]}
        }

        with self.assertRaisesRegex(ValueError, "allow_recycle=True"):
            await self.controller.place("cube_4")

        self.assertFalse(any(call[0] == "place_entity" for call in self.session.calls))

    async def test_place_requires_a_boolean_recycle_opt_in(self):
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            await self.controller.place("pad_A", allow_recycle=1)  # type: ignore[arg-type]

    async def test_place_reports_expected_source_recycling(self):
        self.session.state.robot_status = {
            "robot": {"status": "holding", "held_entity_ids": ["cube_4"]}
        }
        self.session.state.scene_entities["cube_4"].visible = False
        self.session.state.scene_entities["cube_4"].state.parent_pad_id = None

        response = await self.controller.place("pad_A", allow_recycle=True)

        self.assertEqual(response["placement"]["status"], "recycled")

    async def test_place_reports_unexpected_scene_postcondition(self):
        self.session.state.robot_status = {
            "robot": {"status": "holding", "held_entity_ids": ["cube_4"]}
        }

        response = await self.controller.place("pad_B")

        self.assertEqual(response["status"], "unexpected_place")
        self.assertEqual(response["placement"]["status"], "unexpected")
        self.assertEqual(response["placement"]["actual_parent_pad_id"], "pad_A")

    async def test_place_outer_failure_is_not_masked_by_nested_done(self):
        self.session.state.robot_status = {
            "robot": {"status": "holding", "held_entity_ids": ["cube_4"]}
        }
        self.session.invoke_result = {
            "status": "failed",
            "result": {"status": "done"},
        }

        response = await self.controller.place("pad_B")

        self.assertEqual(response["status"], "place_failed")
        self.assertNotIn("placement", response)

    async def test_unknown_entity_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown entity_id"):
            await self.controller.go_to("not-real")

    async def test_execute_code_runs_guarded_navigation(self):
        response = await self.controller.execute_code(
            'menlo.go_to("pad_B")\nreturn menlo.get_robot_state()'
        )

        self.assertEqual(response["status"], "done")
        self.assertEqual(response["calls"], 2)
        self.assertEqual(self.session.calls[0][0], "go_to")

    async def test_execute_code_aborts_after_unexpected_pick(self):
        self.session.invoke_result = {
            "status": "done",
            "result": {"status": "done", "held": "cube_0", "held_count": 1},
        }

        response = await self.controller.execute_code(
            'menlo.pick("cube_4")\nmenlo.stop()'
        )

        self.assertEqual(response["status"], "action_failed")
        self.assertEqual(response["failed_method"], "pick")
        self.assertEqual([call[0] for call in self.session.calls], ["pick_entity"])

    async def test_execute_code_stop_confirms_motion_stopped(self):
        self.session.state.robot_status = _motion_state(0.0)

        response = await self.controller.execute_code("return menlo.stop()")

        self.assertEqual(response["status"], "done")
        self.assertTrue(response["result"]["stop_confirmation"]["motion_stopped"])

    async def test_execute_code_stop_aborts_when_motion_is_unconfirmed(self):
        busy_state = _motion_state(0.0, runtime_status="busy", vx=0.5)
        self.session.state.robot_status = busy_state

        async def unconfirmed_stop(*args, **kwargs):
            return busy_state, None

        self.controller._wait_for_motion_stop = unconfirmed_stop  # type: ignore[method-assign]

        response = await self.controller.execute_code(
            "menlo.stop()\nreturn menlo.get_robot_state()"
        )

        self.assertEqual(response["status"], "action_failed")
        self.assertEqual(response["failed_method"], "stop")
        self.assertEqual(response["calls"], 1)

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
