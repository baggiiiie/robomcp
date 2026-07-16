"""Compose the focused robot capabilities into one public controller."""

from .code_execution import CodeExecutionMixin
from .manipulation import ManipulationMixin
from .motion import MotionMixin
from .vision import VisionMixin


class MenloRobotController(
    MotionMixin,
    ManipulationMixin,
    VisionMixin,
    CodeExecutionMixin,
):
    """Small public facade composed from focused robot capabilities."""
