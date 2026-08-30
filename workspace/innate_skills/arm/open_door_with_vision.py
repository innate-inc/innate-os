# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Acquire a protruding handle with monocular vision, then pull it."""

import math
import statistics
import time

from innate import (
    Head,
    JointStates,
    MainImage,
    Manipulation,
    Mobility,
    Odometry,
    Skill,
    SkillReturn,
    WristImage,
    resource,
    vision,
)
from innate import gemini as gemlib
from innate.exceptions import ArmFailed, ArmUnhealthy, SkillFailed

from .handle_triangulation import camera_ray_odom, odom_point_to_base, triangulate_rays

_HEAD_TILT_DEG = 0.0
_DOGLEG_ANGLE_RAD = math.radians(30.0)
_DOGLEG_REVERSE_M = -0.12
_MIN_RANGE_M = 0.20
_MAX_RANGE_M = 1.50
_MIN_HANDLE_Z_M = 0.10
_MAX_HANDLE_Z_M = 0.30
_APPROACH_RANGE_M = 0.34
_PREGRASP_OFFSET_M = 0.06
_FINGERTIP_OFFSET_M = 0.05
_APPROACH_PAST_HANDLE_M = 0.01
_WRIST_U = 320.0
_WRIST_V = 350.0
_WRIST_DEADBAND_PX = 45.0
_WRIST_GAIN_M_PER_100PX = 0.025
_WRIST_MAX_STEP_M = 0.02
_WRIST_STEPS = 5
_GRIP_STRENGTH = 0.35
_EMPTY_GRIPPER_J6 = -0.085
_TARE_SAMPLES = 6
_EFFORT_DELTA_LIMIT = 20.0
_STATE_MAX_AGE_S = 0.25
_SEARCH_ARM = [1.5708, -1.2195, 1.5723, 0.06, -0.47]


class OpenDoorWithVision(Skill):
    """Find and grasp a frontal protruding bar/lever handle, then pull it.

    Two Gemini detections separated by a short backward dogleg provide metric
    range without a depth camera. The wrist camera refines lateral/vertical
    alignment. This experimental skill rejects weak triangulation, unreachable
    geometry, excessive approach load, and an unverified grasp.
    """

    manipulation: Manipulation
    mobility: Mobility
    head: Head
    main_image: MainImage
    wrist_image: WristImage
    joint_states: JointStates
    odom: Odometry
    debug_enabled = True
    _handle_description = "the protruding handle directly ahead"
    _frame_index = 0

    @resource
    def _proxy(self):
        return gemlib.make_client()

    def _odom_xyt(self):
        pose = self.mobility.odom_xyt(self.odom)
        if pose is None:
            self.fail("Odometry is required for handle triangulation")
        return pose

    def _detect(self, image, camera: str):
        frame_name = f"{self._frame_index:02d}_{camera}_detection.jpg"
        self._frame_index += 1
        directory = self.debug_directory
        if directory is not None:
            try:
                (directory / frame_name).write_bytes(image.jpeg)
            except Exception as error:  # noqa: BLE001 - observability must not block safety
                self.logger.warning(f"[OpenDoorWithVision] could not save {frame_name}: {error}")
        text = gemlib.ask_image(
            self._proxy,
            image,
            f"Find {self._handle_description!r}: the physical door, drawer, or cabinet "
            "HANDLE the robot should grasp. "
            "Choose a protruding bar or lever handle, not the door panel, hinge, lock, "
            "edge, or an image/reflection of a handle. Return ONLY a JSON list with the "
            'best match first: [{"box_2d":[ymin,xmin,ymax,xmax],'
            '"grasp_point":[y,x]}], normalized 0-1000. Empty list if no graspable '
            "handle is visible.",
            logger=self.logger,
        )
        candidates = vision.parse_det_cands(text)
        if not candidates:
            self.fail(f"Could not identify a graspable handle in the {camera} camera")
        u, v, _grip = candidates[0]
        self.debug_event("handle_detection", camera=camera, pixel=[u, v], response=text, frame=frame_name)
        return u, v

    def _rotate(self, radians):
        before = self._odom_xyt()
        if not self.mobility.rotate_by(self._odom_xyt, radians, logger=self.logger):
            self.fail("Base rotation failed during handle triangulation")
        self.debug_event("base_rotation", requested_rad=radians, before=list(before), after=list(self._odom_xyt()))

    def _drive(self, metres):
        before = self._odom_xyt()
        if not self.mobility.drive(self._odom_xyt, metres, logger=self.logger):
            self.fail("Base motion failed during handle triangulation")
        self.debug_event("base_translation", requested_m=metres, before=list(before), after=list(self._odom_xyt()))

    def _triangulate_handle(self):
        self.sleep(0.8)
        first_pose = self._odom_xyt()
        first_px = self._detect(self.main_image, "head")
        first_ray = camera_ray_odom(*first_px, _HEAD_TILT_DEG, first_pose)

        self._rotate(_DOGLEG_ANGLE_RAD)
        self._drive(_DOGLEG_REVERSE_M)
        self._rotate(-_DOGLEG_ANGLE_RAD)
        self.sleep(0.8)

        second_pose = self._odom_xyt()
        second_px = self._detect(self.main_image, "head")
        second_ray = camera_ray_odom(*second_px, _HEAD_TILT_DEG, second_pose)
        try:
            point, gap, angle = triangulate_rays(first_ray, second_ray)
        except ValueError as error:
            self.fail(str(error))
        relative = odom_point_to_base(point, second_pose)
        distance = math.hypot(relative[0], relative[1])
        if not _MIN_RANGE_M <= distance <= _MAX_RANGE_M:
            self.fail(f"Triangulated handle range {distance:.2f} m is outside the safe envelope")
        if not _MIN_HANDLE_Z_M <= relative[2] <= _MAX_HANDLE_Z_M:
            self.fail(f"Triangulated handle height {relative[2]:.2f} m is outside the arm workspace")
        self.debug_event(
            "handle_triangulated",
            odom_point=list(point),
            base_point=list(relative),
            ray_gap_m=gap,
            ray_angle_deg=angle,
            first_pose=list(first_pose),
            second_pose=list(second_pose),
        )
        return point

    def _position_base(self, point):
        relative = odom_point_to_base(point, self._odom_xyt())
        self._rotate(math.atan2(relative[1], relative[0]))
        relative = odom_point_to_base(point, self._odom_xyt())
        self._drive(math.hypot(relative[0], relative[1]) - _APPROACH_RANGE_M)
        final_odom = self._odom_xyt()
        relative = odom_point_to_base(point, final_odom)
        accepted = 0.28 <= relative[0] <= 0.40 and abs(relative[1]) <= 0.05
        self.debug_event(
            "base_position_result",
            odom=list(final_odom),
            handle_base=list(relative),
            accepted=accepted,
            x_bounds_m=[0.28, 0.40],
            max_abs_y_m=0.05,
        )
        if not accepted:
            self.fail("Base could not place the handle inside the arm's safe workspace")
        self.debug_event("base_positioned", handle_base=list(relative))
        return relative

    def _wrist_align(self, target):
        x = max(0.28, min(0.38, target[0] - _PREGRASP_OFFSET_M))
        y, z = target[1], target[2]
        self.manipulation.torque_on()
        self.manipulation.gripper_open(duration=1.0)
        self.manipulation.move_to(x, y, z, duration=2.0)
        for step in range(_WRIST_STEPS):
            self.sleep(0.5)
            u, v = self._detect(self.wrist_image, "wrist")
            err_u, err_v = u - _WRIST_U, v - _WRIST_V
            self.debug_event("wrist_alignment", step=step, pixel=[u, v], error=[err_u, err_v], pose=[x, y, z])
            if abs(err_u) <= _WRIST_DEADBAND_PX and abs(err_v) <= _WRIST_DEADBAND_PX:
                return x, y, z
            dy = max(-_WRIST_MAX_STEP_M, min(_WRIST_MAX_STEP_M, -_WRIST_GAIN_M_PER_100PX * err_u / 100.0))
            dz = max(-_WRIST_MAX_STEP_M, min(_WRIST_MAX_STEP_M, -_WRIST_GAIN_M_PER_100PX * err_v / 100.0))
            y += dy
            z = max(_MIN_HANDLE_Z_M, min(_MAX_HANDLE_Z_M, z + dz))
            self.manipulation.move_to(x, y, z, duration=0.7)
        self.fail("Wrist camera could not align the gripper with the handle")

    def _effort(self):
        age = time.monotonic() - self.joint_states.received_at
        if self.joint_states.received_at <= 0.0 or age > _STATE_MAX_AGE_S:
            self.fail("Arm effort feedback is stale during handle approach")
        values = tuple(float(value) for value in self.joint_states.effort[:5])
        if len(values) != 5 or not all(math.isfinite(value) for value in values):
            self.fail("Arm effort feedback is unavailable during handle approach")
        return values

    def _grasp(self, pregrasp, handle_x):
        baseline_samples = []
        for _ in range(_TARE_SAMPLES):
            baseline_samples.append(self._effort())
            self.sleep(0.04)
        baseline = tuple(statistics.median(sample[j] for sample in baseline_samples) for j in range(5))
        x, y, z = pregrasp
        # x is the EE link, while the fingertips extend ahead of it. Move the
        # fingers just beyond the triangulated handle plane, never the wrist.
        final_x = max(x, min(0.39, handle_x - _FINGERTIP_OFFSET_M + _APPROACH_PAST_HANDLE_M))
        while x + 1e-6 < final_x:
            x = min(final_x, x + 0.005)
            self.manipulation.move_to(x, y, z, duration=0.35, tolerance_xy=None, tolerance_z=None)
            effort = self._effort()
            delta = max(abs(effort[j] - baseline[j]) for j in range(5))
            self.debug_event("grasp_approach", pose=[x, y, z], effort=list(effort), effort_delta=delta)
            if delta > _EFFORT_DELTA_LIMIT:
                self.fail("Unexpected contact load while approaching the handle")
        self.check_cancelled()
        self.manipulation.gripper_close(_GRIP_STRENGTH, duration=1.0)
        time.sleep(0.5)
        positions = self.joint_states.position
        j6 = float(positions[5]) if len(positions) > 5 else None
        held = j6 is not None and j6 > _EMPTY_GRIPPER_J6 + 0.02
        self.debug_event("grasp_verified", gripper_position=j6, held=held)
        if not held:
            self.fail("The gripper closed without capturing the handle")

    def execute(
        self,
        handle_description: str = "the protruding handle directly ahead",
        pull_distance_m: float = 0.05,
    ) -> SkillReturn:
        """Locate, grasp, and pull a frontal protruding handle."""
        if self._proxy is None:
            self.fail("Innate proxy not configured (INNATE_SERVICE_KEY)")
        if not 0.01 <= pull_distance_m <= 0.20:
            self.fail("pull_distance_m must be between 0.01 and 0.20")
        if not handle_description.strip():
            self.fail("handle_description must not be empty")
        self._handle_description = handle_description.strip()
        self._frame_index = 0
        try:
            self.debug_event(
                "acquisition_started",
                handle_description=self._handle_description,
                pull_distance_m=pull_distance_m,
                dogleg_angle_deg=math.degrees(_DOGLEG_ANGLE_RAD),
                dogleg_reverse_m=_DOGLEG_REVERSE_M,
            )
            self.head.set_position(int(_HEAD_TILT_DEG))
            self.manipulation.torque_on()
            self.manipulation.gripper_open(duration=1.0)
            self.manipulation.move_joints(_SEARCH_ARM, duration=3.0)
            point = self._triangulate_handle()
            target = self._position_base(point)
            pregrasp = self._wrist_align(target)
            self._grasp(pregrasp, target[0])
            if self.skills is None:
                self.fail("Skill composition is unavailable for the pull handoff")
            result = self.skills.run(
                "innate-os/pull_held_handle",
                timeout=35.0,
                distance_m=pull_distance_m,
                direction_x=-1.0,
                direction_y=0.0,
                direction_z=0.0,
            )
            if not result.ok:
                raise SkillFailed(result.message)
            return f"Located, grasped, and pulled the handle {pull_distance_m:.2f} m"
        except (ArmFailed, ArmUnhealthy) as error:
            self.fail(str(error))
        finally:
            self.mobility.stop()
            self.manipulation.stream_stop()
            self.head.set_position(0)
