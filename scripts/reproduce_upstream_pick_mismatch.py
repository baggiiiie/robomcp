"""Reproduce an exact-pick mismatch through the Menlo SDK, bypassing MCP.

The script creates a temporary robot, prints a viewer URL, and waits for that
viewer to join. It navigates to pad A and directly asks the runtime to pick the
second visible cube. If the runtime returns a different held entity, the
mismatch occurred below the MCP boundary.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from menlo_robot_sdk import AsyncClient, connect
from menlo_robot_sdk.connection import MenloSession
from menlo_robot_sdk.experimental import generate_room_key


ROOT = Path(__file__).resolve().parents[1]
RCS_URL = "https://api.menlo.ai/rcs"
VIEWER_BASE_URL = "https://sim.menlo.ai"
MODEL = "asimov-v0"


def _json(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    return value


def _distance_xy(first: list[float], second: list[float]) -> float:
    return math.hypot(first[0] - second[0], first[1] - second[1])


async def _wait_for_runtime(
    session: MenloSession, timeout_s: float
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    last_error: Exception | None = None
    while loop.time() < deadline:
        try:
            descriptors = await session.discover_skills()
            by_name = {descriptor.name: descriptor for descriptor in descriptors}
            if {"go_to", "pick_entity"}.issubset(by_name):
                return by_name
        except Exception as exc:
            last_error = exc
        await asyncio.sleep(1)
    detail = f": {type(last_error).__name__}: {last_error}" if last_error else ""
    raise TimeoutError(f"SimpleSim runtime did not become ready{detail}")


def _visible_cube_rows(scene: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, entity in scene.entities.items():
        if not re.fullmatch(r"cube_\d+", key) or not entity.visible:
            continue
        position = list(entity.pose.position)
        rows.append(
            {
                "scene_key": key,
                "entity_id": entity.entity_id,
                "position": position,
            }
        )
    return sorted(rows, key=lambda row: row["position"][1])


async def _navigate_to_pad_a(session: MenloSession, attempts: int = 3) -> Any:
    failures: list[Any] = []
    for attempt in range(1, attempts + 1):
        result = await session.invoke(
            "go_to",
            {"target": {"kind": "entity", "entity_id": "pad_A"}},
            timeout_s=300,
        )
        if result.status == "done":
            return result
        failure = _json(result)
        failures.append(failure)
        print(
            f"Navigation attempt {attempt}/{attempts} failed; retrying from the "
            f"new pose: {json.dumps(failure, sort_keys=True)}",
            file=sys.stderr,
        )
    raise RuntimeError(f"Navigation to pad_A failed after {attempts} attempts: {failures}")


async def reproduce(target_id: str | None, runtime_timeout_s: float) -> int:
    load_dotenv(ROOT / ".env")
    api_key = os.getenv("MENLO_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("MENLO_API_KEY is missing from .env")

    client = AsyncClient(rcs_url=RCS_URL, api_key=api_key)
    session: MenloSession | None = None
    robot_id: str | None = None
    try:
        created = await client.robots.create(
            name="Direct SDK exact-pick reproduction", model=MODEL
        )
        robot_id = created.robot.id
        session = await connect(
            client,
            robot_id,
            worker_names=[],
            rcw_identity_prefix="simplesim",
            join_livekit=True,
        )
        room_key = await generate_room_key(client, robot_id)
        viewer_url = f"{VIEWER_BASE_URL}/?key={room_key}"

        print("MCP boundary: BYPASSED")
        print(f"Robot: {robot_id}")
        print(f"Open this viewer and keep it visible:\n{viewer_url}", flush=True)

        tools = await _wait_for_runtime(session, runtime_timeout_s)
        schemas = {
            name: _json(tools[name])
            for name in ("set_velocity", "pick_entity")
            if name in tools
        }
        print("Live runtime descriptors:")
        print(json.dumps(schemas, indent=2, sort_keys=True))

        scene = await session.state.get("scene_state")
        cubes = _visible_cube_rows(scene)
        if len(cubes) < 2:
            raise RuntimeError(f"Expected at least two visible cubes, found {cubes}")

        requested_id = target_id or cubes[1]["entity_id"]
        requested = next(
            (cube for cube in cubes if cube["entity_id"] == requested_id), None
        )
        if requested is None:
            choices = ", ".join(cube["entity_id"] for cube in cubes)
            raise ValueError(f"Unknown target {requested_id!r}; visible cubes: {choices}")

        await _navigate_to_pad_a(session)

        state_before = await session.state.get("robot_status")
        robot_position = list(state_before.robot.pose.position)
        distances = {
            cube["entity_id"]: _distance_xy(robot_position, cube["position"])
            for cube in cubes
        }
        print("Direct SDK request:")
        print(
            json.dumps(
                {
                    "requested_entity_id": requested_id,
                    "robot_position": robot_position,
                    "cube_distances_xy": distances,
                },
                indent=2,
                sort_keys=True,
            )
        )

        pick_result = await session.invoke(
            "pick_entity",
            {"target": {"kind": "entity", "entity_id": requested_id}},
            timeout_s=60,
        )
        pick_payload = _json(pick_result)
        if pick_result.status != "done":
            raise RuntimeError(f"Direct pick action failed: {pick_payload}")
        nested_result = pick_payload.get("result") or {}
        if not isinstance(nested_result, dict):
            raise RuntimeError(f"Direct pick returned an invalid result: {pick_payload}")
        actual_id = nested_result.get("held")
        if not actual_id:
            state_after = await session.state.get("robot_status")
            held_ids = list(state_after.robot.held_entity_ids)
            actual_id = held_ids[0] if held_ids else None
        if not actual_id:
            raise RuntimeError(
                "Direct pick completed without identifying a held entity: "
                f"{pick_payload}"
            )

        evidence = {
            "transport": "menlo_robot_sdk.MenloSession.invoke",
            "mcp_imported": False,
            "requested_entity_id": requested_id,
            "held_entity_id": actual_id,
            "runtime_result": pick_payload,
        }
        print("Direct runtime result:")
        print(json.dumps(evidence, indent=2, sort_keys=True))

        if actual_id != requested_id:
            print(
                "UPSTREAM_MISMATCH_REPRODUCED: the direct runtime held "
                f"{actual_id!r} after the SDK requested {requested_id!r}."
            )
            return 0

        print(
            "MISMATCH_NOT_REPRODUCED: the runtime held the requested entity. "
            "The upstream behavior may be intermittent or fixed."
        )
        return 1
    finally:
        cleanup_errors: list[str] = []
        if session is not None:
            try:
                await session.disconnect(delete_session=True)
            except Exception as exc:
                cleanup_errors.append(f"disconnect: {type(exc).__name__}: {exc}")
        if robot_id is not None:
            try:
                await client.robots.delete(robot_id)
            except Exception as exc:
                cleanup_errors.append(f"delete robot: {type(exc).__name__}: {exc}")
        try:
            await client.aclose()
        except Exception as exc:
            cleanup_errors.append(f"close client: {type(exc).__name__}: {exc}")
        for error in cleanup_errors:
            print(f"Cleanup warning: {error}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        help="Exact cube entity ID. Defaults to the second visible cube on pad A.",
    )
    parser.add_argument(
        "--runtime-timeout",
        type=float,
        default=180,
        help="Seconds to wait for the printed viewer to join (default: 180).",
    )
    args = parser.parse_args()
    return asyncio.run(reproduce(args.target, args.runtime_timeout))


if __name__ == "__main__":
    raise SystemExit(main())
