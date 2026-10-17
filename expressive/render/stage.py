"""A VirtualMars world posed kinematically or driven through its servos, seen from fixed viewpoints.

Views are fixed in the anchor frame (where the robot stood at the start), because the person the
robot expresses to stands still: advance and orient must read as the robot moving, not the camera.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import _core  # noqa: F401  (puts brain_client and mars_sim_driver on sys.path)
import mujoco
import numpy as np
from mars_sim_driver.core import VirtualMars
from mars_sim_driver.environments import Environment
from numpy.typing import NDArray

from brain_client.expressive.basis import Act

ARM_JOINTS = ("joint1", "joint2", "joint3", "joint4", "joint5", "joint6")


@dataclass(frozen=True)
class View:
    eye: tuple[float, float, float]
    target: tuple[float, float, float]
    fovy: float


VIEWS: dict[str, View] = {
    "front": View((1.4, 0.0, 1.3), (0.08, -0.02, 0.18), 17.0),
    "three-quarter": View((1.3, -0.85, 0.88), (0.12, -0.02, 0.2), 22.0),
    "profile": View((0.12, -1.35, 0.3), (0.12, 0.0, 0.2), 30.0),
}


class Stage:
    def __init__(self, env: str = "void", size: tuple[int, int] = (640, 480)) -> None:
        self.sim = VirtualMars(render_wh=size, environment=Environment.load(env))
        model = self.sim.model
        self._qpos = {name: model.jnt_qposadr[model.joint(f"robot_{name}").id] for name in (*ARM_JOINTS, "joint_head")}
        self._mimic = model.jnt_qposadr[model.joint("robot_joint6M").id]
        self._base = {k: model.jnt_qposadr[model.joint(f"robot_base_{k}").id] for k in ("x", "y", "yaw")}
        self.anchor = self.sim.pose()
        self._segmenter: mujoco.Renderer | None = None
        arm_bodies = {i for i in range(model.nbody) if model.body(i).name.startswith("robot_link")}
        self._arm_geoms = np.array([model.geom_bodyid[g] in arm_bodies for g in range(model.ngeom)])

    def pose(self, q: NDArray[np.float64]) -> None:
        """Place the robot exactly at actuator vector ``q`` (no physics, no servo sag)."""
        d = self.sim.data
        for i, name in enumerate(ARM_JOINTS):
            d.qpos[self._qpos[name]] = q[i]
        d.qpos[self._mimic] = -q[Act.J6]
        d.qpos[self._qpos["joint_head"]] = math.radians(q[Act.HEAD_DEG])
        x0, y0, yaw0 = self.anchor
        d.qpos[self._base["x"]] = x0 + q[Act.BASE_X] * math.cos(yaw0)
        d.qpos[self._base["y"]] = y0 + q[Act.BASE_X] * math.sin(yaw0)
        d.qpos[self._base["yaw"]] = yaw0 + q[Act.BASE_YAW]
        mujoco.mj_forward(self.sim.model, d)

    def command(self, q: NDArray[np.float64]) -> None:
        """Send actuator vector ``q`` to the servos (arm, head); the base is driven by ``Playback``."""
        for i, name in enumerate(ARM_JOINTS):
            self.sim.set_joint_target(name, float(q[i]))
        self.sim.set_joint_target("joint_head", math.radians(float(q[Act.HEAD_DEG])))

    def camera(self, view: str) -> mujoco.MjvCamera:
        v = VIEWS[view]
        x0, y0, yaw0 = self.anchor
        c, s = math.cos(yaw0), math.sin(yaw0)

        def world(p: tuple[float, float, float]) -> NDArray[np.float64]:
            return np.array([x0 + c * p[0] - s * p[1], y0 + s * p[0] + c * p[1], p[2]])

        eye, target = world(v.eye), world(v.target)
        ray = target - eye
        cam = mujoco.MjvCamera()
        cam.type = mujoco.mjtCamera.mjCAMERA_FREE
        cam.lookat[:] = target
        cam.distance = float(np.linalg.norm(ray))
        cam.azimuth = math.degrees(math.atan2(ray[1], ray[0]))
        cam.elevation = math.degrees(math.asin(ray[2] / cam.distance))
        self.sim.model.vis.global_.fovy = v.fovy
        return cam

    def shot(self, view: str) -> NDArray[np.uint8]:
        """A frame from one of ``VIEWS``, or ``"main"``: what the robot's own head camera sees."""
        if view == "main":
            return self.sim.render_rgb("main")
        return self.sim.render_rgb(self.camera(view))

    def arm_in_view(self) -> float:
        """Fraction of the head camera's image covered by the arm (0 = the camera sees past it)."""
        if self._segmenter is None:
            self._segmenter = mujoco.Renderer(self.sim.model, height=240, width=320)
            self._segmenter.enable_segmentation_rendering()
        self._segmenter.update_scene(self.sim.data, camera="main")
        segmentation = self._segmenter.render()
        ids, kinds = segmentation[..., 0], segmentation[..., 1]
        geoms = (kinds == mujoco.mjtObj.mjOBJ_GEOM) & (ids >= 0)
        return float(self._arm_geoms[ids[geoms]].sum() / ids.size)
