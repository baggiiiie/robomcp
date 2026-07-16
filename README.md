# Menlo Robot MCP

`robomcp` is a [Model Context Protocol](https://modelcontextprotocol.io) server that
exposes safe, intuitive robot controls for the Menlo SDK to any MCP-compatible coding
agent. It wraps the Menlo SimpleSim runtime with guarded tools that verify their
postconditions, so an agent can navigate, look, pick, place, and run bounded multi-step
plans without touching the low-level SDK.

See [`PROJECT_PROGRESS.md`](PROJECT_PROGRESS.md) for a fuller overview.

## Tools

| Tool | Description |
| --- | --- |
| `start_robot` / `stop_robot` | Create/delete a temporary robot session; `start_robot` returns a `viewer_url`. |
| `get_scene` / `get_robot_state` | Inspect scene entities and the robot's pose, velocity, and held entity. |
| `look` | Capture the camera view, optionally aiming with `yaw_degrees`/`pitch_degrees`. |
| `walk` / `turn` / `stop` | Bounded motion commands with verified stop reporting. |
| `go_to` | Navigate to a scene `entity_id` via Menlo's native A* planner. |
| `pick` / `place` | Grab and place entities, verified against a refreshed scene. |
| `menlo_execute` | Run a bounded, validated multi-step plan over the guarded methods. |

Every motion, pick, and place verifies its postcondition. `go_to` retries at most twice
on `NAVIGATION_STUCK`, only after measured progress. Timed motions report
`timed_out_stopped` only once the robot is confirmed stopped, else
`timed_out_motion_unconfirmed`.

## Set up

Use Python 3.12 so the SDK's `pydantic` deps don't conflict with system Python:

```bash
uv venv --python 3.12 --seed .venv
source .venv/bin/activate
uv pip install -r requirements.txt
cp .env.example .env   # then set MENLO_API_KEY
```

## Run the MCP server

```bash
.venv/bin/python menlo_mcp_server.py
```

It uses MCP's stdio transport and is normally launched by your coding agent. Configure
an MCP client like:

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

Ask the agent to call `start_robot`, open the returned `viewer_url` in Chrome, and keep
that tab visible—the SimpleSim runtime runs in the browser.

## Executable plans

`menlo_execute(code)` runs a small Python-like language over the guarded methods
(`get_robot_state`, `get_scene`, `go_to`, `pick`, `place`, `stop`, `turn`, `walk`).
Calls are synchronous—no `await`:

```python
for target in ["pad_B", "pad_E", "pad_A"]:
    menlo.go_to(target)
return menlo.get_robot_state()
```

It is a restricted interpreter (no imports, function defs, filesystem/network, or
lifecycle calls). Plans are validated before the first robot call and stop on the first
failed postcondition. Default budgets: 20 robot calls, 20 items/loop, 120 statements, 15
minutes.

## More

- Operating guidance: `.agents/skills/menlo-robot-operator/SKILL.md`
- Known limitations: [`ROBOT_TOOL_LIMITATIONS.md`](ROBOT_TOOL_LIMITATIONS.md)
- Color-sorting benchmark: [`SORTING_BENCHMARK.md`](SORTING_BENCHMARK.md)
- Direct SDK walkthrough: `menlo_sdk_simple_learning.ipynb`
- Run checks: `.venv/bin/python -m unittest discover -s tests -v`
