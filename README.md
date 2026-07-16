# Menlo Robot SDK learning project

This notebook is a small, local version of the Menlo SDK tutorial. It creates a temporary simulated robot, opens the browser viewer, discovers the robot's skills, moves it, captures its POV camera, and then cleans up the remote resources.

## Run locally

Use an isolated Python 3.12 environment so the SDK's `pydantic` dependencies do not conflict with system Python:

```bash
uv venv --python 3.12 --seed .venv
source .venv/bin/activate
uv pip install -r requirements.txt
jupyter lab menlo_sdk_simple_learning.ipynb
```

In Jupyter, select the `.venv` Python kernel if prompted. Create a local `.env` file containing:

```text
MENLO_API_KEY=your_key_here
```

Run the cells from top to bottom. When the viewer link appears, open it in Chrome and keep the tab visible while movement commands run. Always run the cleanup cell when finished.

The `.env`, `.venv`, and notebook checkpoints are ignored by Git.
