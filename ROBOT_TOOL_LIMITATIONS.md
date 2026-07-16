# Robot tool limitations and ownership

Scope: `menlo-robot-sdk==0.3.0`, this MCP server, and the SimpleSim sorting
runtime, observed on 2026-07-16.

Most "upstream" issues below are SimpleSim runtime or native-skill semantics,
not bugs in the Python SDK transport. The MCP calls `MenloSession.invoke`
directly and generally forwards the runtime result unchanged.

| Limitation | Owner | Evidence / impact |
| --- | --- | --- |
| No free-space or coordinate placement | Upstream runtime capability | The available native `place_entity` skill accepts an entity target only. Once a cube is picked, its empty source slot has no entity to target, so a true two-item position swap cannot be completed. |
| Placing onto a cube on source `pad_A` can recycle the held cube | Upstream runtime semantics; MCP guardrail gap | A live `place_entity` call targeting the end cube returned `done`, but the held red cube moved to the invisible pool while the blue target stayed in place. The MCP currently permits this hazardous target and does not validate the placement postcondition. |
| `pad_A` is a source conveyor, not neutral storage | Upstream scenario semantics | Placement onto the source advances/recycles the sequence and can change IDs and row order. It cannot restore the original arrangement. |
| No native swap operation | Upstream runtime capability | There is no `swap_entities` skill. MCP orchestration cannot emulate one without coordinate placement or persistent entities representing empty slots. |
| Carry capacity is one | Upstream runtime/scenario | The robot cannot hold the red cube while picking the blue cube; a swap therefore requires a temporary buffer plus addressable empty slots. |
| Exact-ID pick may return a different nearby cube | Upstream runtime bug | `scripts/reproduce_upstream_pick_mismatch.py` bypasses MCP and invokes `pick_entity` through the SDK directly. The MCP adds a useful `unexpected_pick` postcondition check, but cannot prevent the upstream mismatch. |
| Action success is awkward to interpret | MCP response design | `_invoke` returns the SDK envelope plus state without normalizing nested `result.status`/`error`. A tool call can transport successfully while the native action failed, so every caller must inspect nested fields. |
| Placement has no postcondition validation | MCP implementation | Unlike exact `pick`, `place` only forwards the native result. It does not verify that the held item became visible at the intended target or warn when it was recycled. |
| Entity validation is too permissive | MCP implementation | `_validate_entity` checks only that an ID exists. It does not reject invisible pool entities, occupied source targets, or placements whose parent is `pad_A`. |
| Viewer tab is required for the runtime | Upstream SimpleSim architecture; MCP readiness gap | Without a loaded, visible viewer, calls can wait and then fail with `no rcw answering runtime.*`. MCP has no fast readiness probe or automatic recovery beyond skill discovery during actions. |
| Timed velocity commands may outlive an MCP timeout | Upstream action semantics; MCP safety gap | `set_velocity` can remain active after the wrapper times out. The MCP should stop automatically on timeout and verify zero commanded velocity. |
| Navigation can fail after changing pose | Upstream planner behavior; MCP resilience gap | `go_to` may return `NAVIGATION_STUCK` after partial movement. The MCP exposes the result but does not retry from the new pose or summarize progress. |
| Head commands complete before measured convergence | Upstream action semantics; MCP usability gap | `set_head` can return `done` while measured yaw/pitch are still approaching the target, so an immediate camera frame may be misaligned. |
| Camera and scene identity are not fused | MCP capability gap | `get_camera` returns pixels and `get_scene` returns IDs/metadata separately. Appearance-based selection requires the caller to visually identify an object and then map it to an exact entity. |
| One temporary robot per MCP process | MCP design choice | The controller owns a single session and `start_robot` returns `already_running` until it is stopped. This is acceptable for learning, but not multi-robot operation. |

## Recommended changes

1. **MCP safety:** reject or require an explicit `allow_recycle` flag for
   placements resolving to `pad_A`; reject invisible and occupied targets.
2. **MCP correctness:** normalize native action status and add placement
   postcondition checks comparable to exact-pick validation.
3. **Upstream capability:** add `place_at_pose`, persistent slot entities, or a
   native `swap_entities` skill. This is required for a real table-position swap.
4. **MCP resilience:** stop on velocity timeout, add a runtime readiness probe,
   wait for head convergence, and optionally retry stuck navigation with limits.
5. **Upstream fix:** make exact entity picks honor the requested ID or return a
   failure instead of silently holding a different entity.

Until coordinate/slot placement exists, the safe supported workflow is navigation,
camera/scene inspection, verified picking, and placement onto explicit destination
pads. Rearranging source-table slots is not supported.
