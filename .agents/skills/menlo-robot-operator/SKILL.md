---
name: menlo-robot-operator
description: Operate and diagnose this project's menlo-robot MCP for navigation, camera-guided object selection, pickup, placement, and runtime failures.
---

# Menlo Robot Operator

Use the project-scoped `menlo-robot` MCP tools for robot tasks in this repository.

## Start and inspect

1. Call `get_robot_state` first.
2. If it reports that the robot is not running, call `start_robot` only when the user asked to create or operate a robot.
3. Give the user the returned viewer URL and wait for them to open the current robot's viewer before scene actions.
4. Call `get_scene` to confirm that the runtime is ready.

## Interpret every action result

- Do not treat MCP `isError: false` as action success by itself. Inspect the nested action `result.status` and `result.error`.
- After navigation, confirm the nested status is `done`. A failed `go_to` can still change the robot pose.
- `go_to` automatically retries `NAVIGATION_STUCK` at most twice when measured progress toward the target is at least 0.1 m and the previous navigation is confirmed inactive with zero commanded velocity. It cancels and verifies motion after a navigation timeout. Inspect `navigation.history`; do not add further retries after `navigation_stuck`.

## Executable high-confidence plans

Use `menlo_execute` when several steps are deterministic from known entity IDs or
structured state. It runs a restricted Python subset with synchronous-looking calls
(no `await`) to `menlo.get_robot_state`, `get_scene`, `go_to`, `pick`, `place`,
`stop`, `turn`, and `walk`. Assignments, bounded `for` loops, `if`, `assert`, and
`return` are supported.

Keep uncertainty boundaries outside the plan:

- Do not use it for startup or shutdown; the viewer handshake remains interactive.
- Do not use scene metadata alone to satisfy a visual request. Call `look` directly,
  interpret the image, map the object to an exact ID, then execute the next known
  segment.
- A guarded navigation, pick, placement, stop, or timed-motion failure aborts all
  remaining calls. Inspect top-level `status`, `failed_method`, and the ordered
  `trace`; resolve the failure before submitting a new segment.
- Prefer one short coherent segment over filling the operation or loop budgets.

## Find and pick an object by appearance

Use this sequence for color or visual requests:

1. Reach the requested area with `go_to`.
2. Call `look` before using scene color metadata to select an object.
3. If the target is absent, scan with `look` using a small set of deliberate yaw positions. In SimpleSim, positive pitch looks down and negative pitch looks up; use a modest positive pitch (for example, 15–18 degrees) for the conveyor or pad. The MCP rejects downward pitch above +20 degrees because the current model saturates near +19.3 degrees despite advertising a larger range. Within the same tool call, `look` estimates a convergence window from the requested angular travel, polls measured head state, fails early if progress stalls, and captures only after convergence.
4. If head scanning is insufficient, reposition to a midpoint entity or another safe viewpoint, then capture again.
5. Once the target is visibly identified, refresh `get_scene` and map the observed object to an exact entity ID.
6. Approach that exact entity with `go_to` before `pick`; otherwise the runtime may choose a nearer reachable object.
7. Call `pick` with the exact ID and verify both the returned `held` value and `robot_state.robot.held_entity_ids`.
8. Treat top-level `status: unexpected_pick` as a failed postcondition. Report requested and actual IDs. Do not automatically place the unexpected object because placement can mutate the source sequence.

The special `pick("cube")` alias intentionally accepts any nearest reachable cube and does not require exact-ID validation.

## Velocity safety

Prefer `go_to` over `walk` and `turn` for routine movement.

Menlo velocity commands are persistent upstream: a command can keep moving until another velocity or cancellation arrives. The current MCP sends `duration_s`, but a timed `walk` or `turn` can time out while the runtime action remains active.

If a velocity tool times out:

1. The MCP calls `stop` automatically and polls robot state.
2. Continue only when the returned status is `timed_out_stopped` and `motion_stopped` is true.
3. Treat `timed_out_motion_unconfirmed` as unsafe: call `stop` again and inspect `get_robot_state` before any other action.

Do not retry a timed velocity command after a timeout without first stopping it.

## Placement semantics

- `place` requires an exact scene entity or pad ID; there is no neutral free-space drop tool.
- Placing onto source `pad_A` recycles the held cube, advances the source sequence, and can shift entity positions. It does not restore the original arrangement. The MCP rejects this by default; pass `allow_recycle=true` only when recycling is intentional.
- Inspect the returned `placement.status`. Treat `unexpected_place` as a failed postcondition. The MCP refreshes the scene after placement, but call `get_scene` again before referring to cube IDs or row order.

## Diagnostics

For an upstream exact-pick mismatch, run `scripts/reproduce_upstream_pick_mismatch.py`. It uses `MenloSession.invoke` directly and never imports or calls the MCP server. Compare its direct runtime result with the MCP forwarding assertion in `tests/test_menlo_mcp_server.py`.
