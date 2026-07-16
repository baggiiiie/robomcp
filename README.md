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

- `get_scene`, `get_robot_state`, and `get_camera`
- `walk`, `turn`, `go_to`, and `stop`
- `pick`, `place`, and `aim_head`
- `stop_robot` to delete the temporary robot and release the session

`go_to(entity_id)` passes an exact scene entity ID to Menlo's native `go_to` skill.
Menlo performs the A* route planning and obstacle avoidance; the MCP server does not
plan the route itself.

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
