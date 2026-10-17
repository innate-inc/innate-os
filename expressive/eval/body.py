"""What a clip physically does to the body, measured kinematically: how far the gripper travels from the
fold, how high it gets, and the head and base ranges. The yardstick for "is there anything to see"."""

from __future__ import annotations

import math
from dataclasses import dataclass

import _core  # noqa: F401
import numpy as np
from render.stage import Stage

from brain_client.expressive.basis import Act, Basis
from brain_client.expressive.motion import Clip

STEP = 2  # every other 25 Hz frame


@dataclass(frozen=True)
class Excursion:
    tip_cm: float  # farthest the gripper gets from where it starts, base motion removed
    tip_height_cm: tuple[float, float]
    head_deg: tuple[float, float]
    base_yaw_deg: float  # range
    base_x_cm: float  # range


class Body:
    def __init__(self, basis: Basis | None = None) -> None:
        self.basis = basis or Basis.load()
        self.stage = Stage(size=(32, 32))
        model = self.stage.sim.model
        self._tip = model.body("robot_ee_link").id
        self._base = model.body("robot_base_link").id

    def _tip_at(self, q: np.ndarray) -> np.ndarray:
        self.stage.pose(q)
        xpos = self.stage.sim.data.xpos
        return xpos[self._tip] - xpos[self._base] * np.array([1.0, 1.0, 0.0])

    def measure(self, clip: Clip) -> Excursion:
        actuators = self.basis.limit_frames(clip.actuator_frames(self.basis), 1.0 / clip.fps)
        tips = np.array([self._tip_at(q) for q in actuators[::STEP]])
        reach = np.linalg.norm(tips - tips[0], axis=1)
        head = actuators[:, Act.HEAD_DEG]
        return Excursion(
            tip_cm=float(100 * reach.max()),
            tip_height_cm=(float(100 * tips[:, 2].min()), float(100 * tips[:, 2].max())),
            head_deg=(float(head.min()), float(head.max())),
            base_yaw_deg=float(math.degrees(np.ptp(actuators[:, Act.BASE_YAW]))),
            base_x_cm=float(100 * np.ptp(actuators[:, Act.BASE_X])),
        )
