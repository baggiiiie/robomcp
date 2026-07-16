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
- If `go_to` reports `NAVIGATION_STUCK`, refresh state and retry from the new pose once or twice. Stop escalating if the pose is not making meaningful progress.

## Find and pick an object by appearance

Use this sequence for color or visual requests:

1. Reach the requested area with `go_to`.
2. Call `get_camera` before using scene color metadata to select an object.
3. If the target is absent, scan with `aim_head` using a small set of deliberate yaw positions, keeping a downward pitch when looking at the conveyor or pad.
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

1. Assume the robot may still be moving.
2. Call `get_robot_state` immediately.
3. If the runtime is busy or command velocity is nonzero, call `stop` immediately.
4. Confirm the runtime is ready and commanded velocities are zero before any other action.

Do not retry a timed velocity command after a timeout without first stopping it.

## Placement semantics

- `place` requires an exact scene entity or pad ID; there is no neutral free-space drop tool.
- Placing onto source `pad_A` recycles the held cube, advances the source sequence, and can shift entity positions. It does not restore the original arrangement.
- Refresh `get_scene` after every placement before referring to cube IDs or row order.

## Diagnostics

For an upstream exact-pick mismatch, run `scripts/reproduce_upstream_pick_mismatch.py`. It uses `MenloSession.invoke` directly and never imports or calls the MCP server. Compare its direct runtime result with the MCP forwarding assertion in `tests/test_menlo_mcp_server.py`.
