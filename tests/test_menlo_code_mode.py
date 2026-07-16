from __future__ import annotations

import unittest
from typing import Any

from menlo_code_mode import MenloCodeExecutor, PlanActionError


class MenloCodeExecutorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.calls: list[tuple[str, list[Any], dict[str, Any]]] = []

    async def call(
        self, method: str, arguments: list[Any], keywords: dict[str, Any]
    ) -> Any:
        self.calls.append((method, arguments, keywords))
        if method == "get_robot_state":
            return {"robot": {"held_entity_ids": []}}
        return {"status": "ok", "method": method}

    async def test_executes_a_loop_as_an_ordered_plan(self):
        result = await MenloCodeExecutor(self.call).execute(
            """
for target in ["pad_B", "pad_E", "pad_A"]:
    menlo.go_to(target)
return menlo.get_robot_state()
"""
        )

        self.assertEqual(result["status"], "done")
        self.assertEqual(
            [(method, arguments) for method, arguments, _ in self.calls],
            [
                ("go_to", ["pad_B"]),
                ("go_to", ["pad_E"]),
                ("go_to", ["pad_A"]),
                ("get_robot_state", []),
            ],
        )
        self.assertEqual(result["calls"], 4)

    async def test_validates_the_whole_plan_before_any_side_effect(self):
        result = await MenloCodeExecutor(self.call).execute(
            'menlo.go_to("pad_B")\nimport os'
        )

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.calls, [])
        self.assertIn("Import", result["error"])

    async def test_rejects_non_json_literals_before_any_side_effect(self):
        result = await MenloCodeExecutor(self.call).execute(
            'menlo.go_to("pad_B")\nreturn b"not-json"'
        )

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.calls, [])
        self.assertIn("bytes", result["error"])

    async def test_rejects_loop_control_outside_a_loop(self):
        result = await MenloCodeExecutor(self.call).execute("break")

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.calls, [])
        self.assertIn("inside a loop", result["error"])

    async def test_action_failure_aborts_remaining_calls(self):
        async def failing_call(method, arguments, keywords):
            self.calls.append((method, arguments, keywords))
            if method == "pick":
                raise PlanActionError("pick", "held the wrong cube")
            return {"status": "ok"}

        result = await MenloCodeExecutor(failing_call).execute(
            'menlo.go_to("cube_1")\nmenlo.pick("cube_1")\nmenlo.go_to("pad_B")'
        )

        self.assertEqual(result["status"], "action_failed")
        self.assertEqual(result["failed_method"], "pick")
        self.assertEqual([call[0] for call in self.calls], ["go_to", "pick"])
        self.assertEqual(result["trace"][-1]["status"], "failed")

    async def test_call_budget_prevents_an_extra_operation(self):
        result = await MenloCodeExecutor(self.call, max_calls=2).execute(
            "menlo.stop()\nmenlo.stop()\nmenlo.stop()"
        )

        self.assertEqual(result["status"], "execution_failed")
        self.assertEqual(len(self.calls), 2)
        self.assertIn("operation budget", result["error"])

    async def test_supports_state_based_selection(self):
        async def scene_call(method, arguments, keywords):
            self.calls.append((method, arguments, keywords))
            return {
                "entities": {
                    "cube_1": {"state": {"color": "blue"}},
                    "cube_2": {"state": {"color": "red"}},
                }
            }

        result = await MenloCodeExecutor(scene_call).execute(
            """
scene = menlo.get_scene()
matches = []
for entity_id in sorted(scene["entities"]):
    entity = scene["entities"][entity_id]
    if entity["state"]["color"] == "red":
        matches = matches + [entity_id]
return matches
"""
        )

        self.assertEqual(result["status"], "done")
        self.assertEqual(result["result"], ["cube_2"])

    async def test_rejects_oversized_integer_computation(self):
        result = await MenloCodeExecutor(self.call).execute(
            "value = 1\nfor ignored in range(20):\n    value = value * value + 2\nreturn value"
        )

        self.assertEqual(result["status"], "execution_failed")
        self.assertIn("4096-bit budget", result["error"])

    async def test_builtin_iterables_are_bounded_without_full_materialization(self):
        async def large_result(method, arguments, keywords):
            self.calls.append((method, arguments, keywords))
            return range(1_000_000_000)

        result = await MenloCodeExecutor(large_result).execute(
            "values = menlo.get_scene()\nreturn sum(values)"
        )

        self.assertEqual(result["status"], "execution_failed")
        self.assertIn("sum input", result["error"])


if __name__ == "__main__":
    unittest.main()
