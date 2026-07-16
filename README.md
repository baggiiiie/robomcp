# Menlo Robot SDK learning project

This project contains two small ways to learn the Menlo SDK:

- `menlo_sdk_simple_learning.ipynb` walks through the SDK directly.
- `menlo_mcp_server.py` exposes intuitive robot controls to any MCP-compatible coding agent.

## Set up

Use Python 3.12 so the SDK's `pydantic` dependencies do not conflict with system Python:

```bash
uv venv --python 3.12 --seed .venv
source .venv/bin/activate
uv pip install -r requirements.txt
cp .env.example .env
```

Put your Menlo API key in `.env`:

```text
MENLO_API_KEY=your_key_here
```

## Run the MCP server

To confirm that the server launches, run:

```bash
.venv/bin/python menlo_mcp_server.py
```

The server uses MCP's stdio transport, so it waits silently for an MCP client. It is
normally launched by your coding agent, not used directly from the terminal.

Configure an MCP client with an entry like this (replace the paths if you move the project):

```json
{
  "mcpServers": {
    "menlo-robot": {
      "command": "/private/tmp/menlo/.venv/bin/python",
      "args": ["/private/tmp/menlo/menlo_mcp_server.py"]
    }
  }
}
```

Then ask the agent to call `start_robot`. Open the returned `viewer_url` in Chrome
and keep that tab visible: the SimpleSim runtime runs in the browser. Once the scene
has loaded, the agent can use:

- `get_scene`, `get_robot_state`, and `look`
- `walk`, `turn`, `go_to`, and `stop`
- `pick` and verified `place`
- `menlo_execute` for bounded, high-confidence multi-step plans
- `stop_robot` to delete the temporary robot and release the session

`look(yaw_degrees?, pitch_degrees?)` replaces the separate camera and head tools.
When angles are supplied it waits for measured head convergence before capturing;
with no angles it captures the current view. `place` rejects source-table recycling
unless `allow_recycle=true` is explicit, then refreshes the scene to verify the
postcondition.

`go_to(entity_id)` passes an exact scene entity ID to Menlo's native `go_to` skill.
Menlo performs the A* route planning and obstacle avoidance. If navigation reports
`NAVIGATION_STUCK`, the MCP retries at most twice and only after at least 10 cm of
measured progress toward the target, runtime readiness, zero commanded velocity,
and confirmation that navigation is inactive. A navigation timeout is cancelled
and verified with the same stop checks used for velocity commands.

Timed `walk` and `turn` failures trigger an immediate `cancel` followed by state
polling. Their result reports `timed_out_stopped` only after the runtime is ready and
all commanded velocities are zero; otherwise it reports
`timed_out_motion_unconfirmed`.

## Executable plans

`menlo_execute(code)` runs a deliberately small Python-like language over the same
guarded controller methods. Calls look synchronous—do not write `await`:

```python
for target in ["pad_B", "pad_E", "pad_A"]:
    menlo.go_to(target)
return menlo.get_robot_state()
```

The allowed methods are `get_robot_state`, `get_scene`, `go_to`, `pick`, `place`,
`stop`, `turn`, and `walk`. Plans may assign variables, branch, loop over bounded
collections, assert conditions, and return JSON data. The entire plan is validated
before its first robot call, and it stops immediately when an action fails its MCP
postcondition. Results include an ordered call trace.

This is a restricted interpreter, not Python `exec`: imports, function definitions,
arbitrary object access, filesystem/network access, camera capture, and robot
lifecycle calls are unavailable. Use direct `look` calls whenever the model must
interpret an image, then submit another executable segment after the uncertainty is
resolved. Default budgets cap a plan at 20 robot calls, 20 items per loop, 120
executed statements, and 15 minutes.

The project-local operating guidance is in
`.agents/skills/menlo-robot-operator/SKILL.md`. It is scoped to this repository and
is discovered only when Codex is working in this project.

Known tool and runtime shortcomings, their ownership, and recommended fixes are
documented in [`ROBOT_TOOL_LIMITATIONS.md`](ROBOT_TOOL_LIMITATIONS.md).

## Reproduce an upstream exact-pick mismatch

Run the direct-SDK reproduction without the MCP server:

```bash
.venv/bin/python scripts/reproduce_upstream_pick_mismatch.py
```

Open the printed viewer URL and keep it visible. The script prints the live runtime
schemas, navigates to pad A, and calls `MenloSession.invoke("pick_entity", ...)`
directly. It exits successfully only when the runtime holds an entity other than the
exact requested ID, demonstrating that the mismatch occurred below the MCP boundary.
The temporary robot is deleted during cleanup.

## Run the notebook

```bash
source .venv/bin/activate
jupyter lab menlo_sdk_simple_learning.ipynb
```

Select **Python 3.12 (Menlo SDK)** if prompted, then run the cells from top to bottom.
Always run the cleanup cell when finished.

## Run the checks

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The `.env`, `.venv`, Python caches, and notebook checkpoints are ignored by Git.
