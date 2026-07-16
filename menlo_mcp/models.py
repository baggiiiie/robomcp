"""Type Menlo payloads and expose failures hidden in nested action results."""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MenloModel(BaseModel):
    """Permissive model for the evolving SDK payloads we actually consume."""

    model_config = ConfigDict(extra="allow")


class Pose(MenloModel):
    position: list[float] = Field(default_factory=list)

    @property
    def xy(self) -> tuple[float, float] | None:
        if len(self.position) < 2:
            return None
        return self.position[0], self.position[1]


class EntityState(MenloModel):
    parent_pad_id: str | None = None


class Entity(MenloModel):
    entity_id: str
    visible: bool = True
    attached_to: str | None = None
    pose: Pose = Field(default_factory=Pose)
    state: EntityState = Field(default_factory=EntityState)


class Scene(MenloModel):
    entities: dict[str, Entity] = Field(default_factory=dict)

    def find(self, entity_id: str) -> Entity:
        entity = self.entities.get(entity_id)
        if entity is None:
            entity = next(
                (
                    item
                    for item in self.entities.values()
                    if item.entity_id == entity_id
                ),
                None,
            )
        if entity is None:
            choices = sorted(
                set(self.entities)
                | {item.entity_id for item in self.entities.values()}
            )
            raise ValueError(
                f"Unknown entity_id {entity_id!r}. Scene entities include: "
                + ", ".join(choices[:20])
            )
        return entity


class VelocityCommand(MenloModel):
    vx: float = 0.0
    vy: float = 0.0
    wz: float = 0.0


class NavigationState(MenloModel):
    active: bool = False


class HeadState(MenloModel):
    measured: dict[str, float] = Field(default_factory=dict)


class RobotExtra(MenloModel):
    command: VelocityCommand = Field(default_factory=VelocityCommand)
    nav: NavigationState = Field(default_factory=NavigationState)
    head: HeadState = Field(default_factory=HeadState)


class Robot(MenloModel):
    status: str | None = None
    pose: Pose = Field(default_factory=Pose)
    held_entity_ids: list[str] = Field(default_factory=list)
    extra: RobotExtra = Field(default_factory=RobotExtra)


class Runtime(MenloModel):
    status: str | None = None


class RobotState(MenloModel):
    runtime: Runtime = Field(default_factory=Runtime)
    robot: Robot = Field(default_factory=Robot)

    def motion_is_stopped(self, *, require_navigation_inactive: bool = False) -> bool:
        command = self.robot.extra.command
        navigation = self.robot.extra.nav
        nav_stopped = not navigation.active
        if require_navigation_inactive:
            nav_stopped = navigation.active is False
        return (
            self.runtime.status == "ready"
            and nav_stopped
            and max(abs(command.vx), abs(command.vy), abs(command.wz)) <= 1e-4
        )


class ActionPayload(MenloModel):
    status: str | None = None
    error: Any = None
    result: dict[str, Any] | None = None

    @property
    def effective_status(self) -> str | None:
        if self.status and self.status != "done":
            return self.status
        nested = self.result or {}
        return nested.get("status") or self.status

    @property
    def effective_error(self) -> Any:
        nested = self.result or {}
        return nested.get("error", self.error)

    @property
    def error_code(self) -> str | None:
        error = self.effective_error
        if isinstance(error, dict) and error.get("code"):
            return str(error["code"])
        return None


def jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if hasattr(value, "__dict__"):
        return {
            key: jsonable(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return value


def action_payload(response: dict[str, Any]) -> ActionPayload:
    return ActionPayload.model_validate(response.get("result", {}))


def action_error_message(response: dict[str, Any]) -> str | None:
    error = action_payload(response).effective_error
    if isinstance(error, dict):
        code, message = error.get("code"), error.get("message")
        return ": ".join(str(value) for value in (code, message) if value) or None
    return str(error) if error is not None else None


def number(name: str, value: float, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    value = float(value)
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


HEAD_PITCH_MIN_DEGREES = -40.0
HEAD_PITCH_MAX_DEGREES = 20.0
