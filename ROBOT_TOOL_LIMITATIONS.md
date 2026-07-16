# Robot tool limitations and ownership

Scope: `menlo-robot-sdk==0.3.0`, this MCP server, and the SimpleSim sorting
runtime, observed on 2026-07-16.

Most "upstream" issues below are SimpleSim runtime or native-skill semantics,
not bugs in the Python SDK transport. The MCP calls `MenloSession.invoke`
directly and generally forwards the runtime result unchanged.

## What the MCP currently compensates for

Saying that the MCP "compensates for the SDK" is imprecise. The Python SDK mostly
provides transport, state access, and native-skill invocation. Most workarounds are
for SimpleSim or native-skill behavior observed through the SDK.

| Observed upstream behavior | MCP compensation | Implementation |
| --- | --- | --- |
| A successful SDK call can contain a failed native action in nested `status` and `error` fields. | Parse the nested action envelope before treating an operation as successful. | [`menlo_mcp/models.py`](menlo_mcp/models.py), [`menlo_mcp/code_execution.py`](menlo_mcp/code_execution.py) |
| Runtime skills are unavailable until the browser viewer worker has loaded. | Poll skill discovery before the first invocation and provide a viewer-readiness error after a timeout. | [`menlo_mcp/connection.py`](menlo_mcp/connection.py) |
| A timed-out velocity command may continue moving the robot. | Cancel the action, poll state, and report stopped only after runtime readiness and zero commanded velocity. | [`menlo_mcp/motion.py`](menlo_mcp/motion.py) |
| `go_to` can return `NAVIGATION_STUCK` after moving partway toward the target. | Measure progress, confirm navigation is inactive, and retry at most twice when at least 0.1 m of progress was made. | [`menlo_mcp/motion.py`](menlo_mcp/motion.py) |
| An exact-ID `pick_entity` request can pick a different nearby cube. | Compare the requested ID with the returned and held IDs and report `unexpected_pick`. | [`menlo_mcp/manipulation.py`](menlo_mcp/manipulation.py) |
| `place_entity` can report completion without producing the requested scene state. | Refresh the scene and verify that the held entity is visible on the expected destination pad. | [`menlo_mcp/manipulation.py`](menlo_mcp/manipulation.py) |
| Placing on source `pad_A` recycles the cube and mutates the source sequence. | Reject source recycling unless `allow_recycle=true`, then verify the recycled state. | [`menlo_mcp/manipulation.py`](menlo_mcp/manipulation.py) |
| `set_head` can report `done` before the head finishes moving. | Wait five seconds after a successful aim before capturing. | [`menlo_mcp/vision.py`](menlo_mcp/vision.py) |
| The runtime advertises downward pitch beyond what the model can reach. | Reject pitch above +20 degrees instead of waiting for impossible convergence. | [`menlo_mcp/vision.py`](menlo_mcp/vision.py) |

These features are **not** upstream compensations and can be removed independently
if the goal is a thinner wrapper:

- `menlo_execute` and its restricted plan language are an MCP orchestration feature.
- Local Pydantic projections of SDK payloads are an MCP typing/design choice.
- Friendly degrees, speeds, defaults, and simplified entity-ID arguments are an MCP
  interface choice.
- Owning one temporary robot per process is an MCP lifecycle choice.
- Camera pixels still are not fused with scene entity IDs; the MCP documents this
  gap but does not compensate for it.

| Limitation | Owner | Evidence / impact |
| --- | --- | --- |
| No free-space or coordinate placement | Upstream runtime capability | The available native `place_entity` skill accepts an entity target only. Once a cube is picked, its empty source slot has no entity to target, so a true two-item position swap cannot be completed. |
| Placing onto a cube on source `pad_A` can recycle the held cube | Upstream runtime semantics; mitigated in MCP | A live `place_entity` call targeting the end cube returned `done`, but the held red cube moved to the invisible pool while the blue target stayed in place. MCP `place` now rejects source targets unless `allow_recycle=true` and verifies the refreshed scene. |
| `pad_A` is a source conveyor, not neutral storage | Upstream scenario semantics | Placement onto the source advances/recycles the sequence and can change IDs and row order. It cannot restore the original arrangement. |
| No native swap operation | Upstream runtime capability | There is no `swap_entities` skill. MCP orchestration cannot emulate one without coordinate placement or persistent entities representing empty slots. |
| Carry capacity is one | Upstream runtime/scenario | The robot cannot hold the red cube while picking the blue cube; a swap therefore requires a temporary buffer plus addressable empty slots. |
| Exact-ID pick may return a different nearby cube | Upstream runtime bug | `scripts/reproduce_upstream_pick_mismatch.py` bypasses MCP and invokes `pick_entity` through the SDK directly. The MCP adds a useful `unexpected_pick` postcondition check, but cannot prevent the upstream mismatch. |
| Action success is awkward to interpret | MCP response design | `_invoke` returns the SDK envelope plus state without normalizing nested `result.status`/`error`. A tool call can transport successfully while the native action failed, so every caller must inspect nested fields. |
| Generic entity validation is permissive | MCP implementation | Navigation and picking validate only that an ID exists. Placement now additionally rejects invisible, attached, unaddressable, and implicit source-recycling targets. |
| Viewer tab is required for the runtime | Upstream SimpleSim architecture; MCP readiness gap | Without a loaded, visible viewer, calls can wait and then fail with `no rcw answering runtime.*`. MCP has no fast readiness probe or automatic recovery beyond skill discovery during actions. |
| Timed velocity commands may outlive an MCP timeout | Upstream action semantics; mitigated in MCP | `set_velocity` can remain active after the wrapper times out. MCP now cancels automatically and reports success only after runtime readiness and zero commanded velocity are confirmed. |
| Navigation can fail after changing pose | Upstream planner behavior; mitigated in MCP | `go_to` may return `NAVIGATION_STUCK` after partial movement. MCP now retries at most twice only when target-distance progress is at least 0.1 m and prior navigation is confirmed stopped. Navigation timeouts are cancelled and verified, and all attempts are summarized. |
| Camera and scene identity are not fused | MCP capability gap | `look` returns pixels while `get_scene` returns IDs/metadata separately. Appearance-based selection still requires the caller to map a visual object to an exact entity. |
| Head pitch uses positive-down convention | Upstream SimpleSim native-skill convention; documented in MCP | The MCP and SDK forward `set_head.pitch` without changing its sign. In SimpleSim, positive pitch looks down and negative pitch looks up. |
| Advertised downward head range is not reachable | Upstream SimpleSim model/runtime metadata; mitigated in MCP | Runtime state advertises pitch through +40 degrees, but live +25 and +30 degree targets saturated near +19.3 degrees. MCP `look` and head aiming now reject pitch above +20 degrees immediately instead of waiting for impossible convergence. |
| Native head action completion precedes physical movement | Upstream action semantics; mitigated in MCP | `set_head` can report `done` while the head is still moving. MCP `look` waits a fixed five seconds before capture. |
| One temporary robot per MCP process | MCP design choice | The controller owns a single session and `start_robot` returns `already_running` until it is stopped. This is acceptable for learning, but not multi-robot operation. |
| Executable plans cannot inspect images mid-plan | Deliberate MCP safety boundary | `menlo_execute` composes structured reads and guarded actions, but excludes `look`. The model must inspect camera output between plan segments before making an appearance-dependent choice. |
| Executable plans are intentionally not arbitrary Python | Deliberate MCP security boundary | A restricted AST interpreter validates the full plan before motion and enforces call, statement, loop, source, output, and elapsed-time budgets. Imports, user functions, arbitrary attributes, filesystem/network access, lifecycle operations, and raw SDK access are unavailable. |

## Recommended changes

1. **Upstream capability:** add `place_at_pose`, persistent slot entities, or a
   native `swap_entities` skill. This is required for a real table-position swap.
2. **MCP resilience:** add a fast runtime readiness probe and automatic recovery
   for missing SimpleSim workers.
3. **Upstream fix:** make exact entity picks honor the requested ID or return a
   failure instead of silently holding a different entity.

Implemented MCP mitigations: `look` now combines head aiming, a five-second wait,
and capture; `place` now requires explicit source-recycle intent and verifies the
refreshed-scene postcondition; velocity timeouts now trigger cancellation and
zero-velocity verification; stuck navigation retries are bounded and progress-aware;
`menlo_execute` now provides bounded, traceable composition and aborts on failed
action postconditions.

Until coordinate/slot placement exists, the safe supported workflow is navigation,
camera/scene inspection, verified picking, and placement onto explicit destination
pads. Rearranging source-table slots is not supported.
