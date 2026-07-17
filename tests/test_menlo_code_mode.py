from __future__ import annotations

import unittest
from pathlib import Path
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

    async def test_default_budget_runs_complete_benchmark_plan(self):
        colors = {
            "blue": "pad_D",
            "green": "pad_C",
            "red": "pad_B",
            "yellow": "pad_E",
        }
        first_colors = ["blue", "green", "green", "blue", "red", "red", "blue"]
        second_colors = ["red", "green", "blue", "yellow", "yellow"]
        scenes = []
        for prefix, batch in (("cube", first_colors), ("cube_pool", second_colors)):
            scenes.append(
                {
                    "entities": {
                        f"{prefix}_{index}": {
                            "visible": True,
                            "state": {"parent_pad_id": "pad_A", "color": color},
                        }
                        for index, color in enumerate(batch)
                    }
                }
            )

        async def benchmark_call(method, arguments, keywords):
            self.calls.append((method, arguments, keywords))
            if method == "get_scene":
                return scenes.pop(0)
            if method == "place":
                return {
                    "robot_state": {
                        "robot": {"extra": {"sort_benchmark": {"status": "complete"}}}
                    }
                }
            return {"status": "ok"}

        guide = (Path(__file__).parents[1] / "SORTING_BENCHMARK.md").read_text()
        plan = guide.split("```python\n", 1)[1].split("\n```", 1)[0]

        result = await MenloCodeExecutor(benchmark_call).execute(plan)

        self.assertEqual(result["status"], "done")
        self.assertEqual(result["calls"], 50)
        self.assertEqual(result["result"], {"status": "complete"})
        self.assertEqual([method for method, _, _ in self.calls].count("get_scene"), 2)
        self.assertEqual([method for method, _, _ in self.calls].count("place"), 12)
        self.assertEqual(
            [arguments[0] for method, arguments, _ in self.calls if method == "place"],
            [colors[color] for color in first_colors + second_colors],
        )

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
