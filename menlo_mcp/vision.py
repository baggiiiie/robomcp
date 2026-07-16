"""Wait five seconds after aiming and reject unreachable pitch before capture."""

from __future__ import annotations

import asyncio
import math
from typing import Any

from .connection import MenloConnection
from .models import (
    HEAD_PITCH_MAX_DEGREES,
    HEAD_PITCH_MIN_DEGREES,
    action_payload,
    number,
)

HEAD_SETTLE_SECONDS = 5.0


class VisionMixin(MenloConnection):
    async def aim_head(
        self, yaw_degrees: float | None, pitch_degrees: float | None
    ) -> dict[str, Any]:
        if yaw_degrees is None and pitch_degrees is None:
            raise ValueError("Provide yaw_degrees, pitch_degrees, or both")
        parameters = {}
        if yaw_degrees is not None:
            parameters["yaw"] = math.radians(
                number("yaw_degrees", yaw_degrees, -80, 80)
            )
        if pitch_degrees is not None:
            parameters["pitch"] = math.radians(
                number(
                    "pitch_degrees",
                    pitch_degrees,
                    HEAD_PITCH_MIN_DEGREES,
                    HEAD_PITCH_MAX_DEGREES,
                )
            )
        return await self._invoke("set_head", parameters, timeout_s=15)

    async def look(
        self, yaw_degrees: float | None, pitch_degrees: float | None
    ) -> bytes:
        if yaw_degrees is None and pitch_degrees is None:
            return await self.camera()

        response = await self.aim_head(yaw_degrees, pitch_degrees)
        action = action_payload(response)
        if action.effective_status != "done":
            raise RuntimeError(f"Head aim failed: {action.effective_error!r}")
        await asyncio.sleep(HEAD_SETTLE_SECONDS)
        return await self.camera()
