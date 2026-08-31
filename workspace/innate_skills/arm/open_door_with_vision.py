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

from .handle_triangulation import (
    odom_point_to_base,
    stable_vertical_box,
    validate_vertical_box,
    vertical_handle_target,
)

_HEAD_TILT_DEG = 0.0
_MIN_RANGE_M = 0.20
_MAX_RANGE_M = 1.50
_MIN_HANDLE_Z_M = 0.10
_MAX_HANDLE_Z_M = 0.30
_APPROACH_RANGE_M = 0.44
_PREGRASP_X_M = 0.22
_FINGERTIP_OFFSET_M = 0.05
_APPROACH_PAST_HANDLE_M = 0.01
_WRIST_U = 320.0
_WRIST_V = 350.0
_WRIST_DEADBAND_PX = 45.0
_WRIST_GAIN_M_PER_100PX = 0.025
_WRIST_MAX_STEP_M = 0.02
_WRIST_STEPS = 5
_WRIST_FY_PX = 461.0  # 480 / (2 tan(55 deg / 2)); nominal UC684 vertical FOV
_WRIST_STOP_RANGE_M = 0.18
_WRIST_RANGE_DEADBAND_M = 0.015
_WRIST_RANGE_MAX_STEP_M = 0.025
_GRIP_STRENGTH = 0.35
_EMPTY_GRIPPER_J6 = -0.085
_TARE_SAMPLES = 6
_EFFORT_DELTA_LIMIT = 20.0
_STATE_MAX_AGE_S = 0.25
_SEARCH_ARM = [1.5708, -1.2195, 1.5723, 0.06, -0.47]

# Raw 640x480 left-image K from the calibrated MARS stereo camera. The wrist
# module has no CameraInfo publisher, hence the nominal manufacturer FOV above.
_MAIN_FX_PX = 195.36129809912026
_MAIN_FY_PX = 259.5741983485189
_MAIN_CX_PX = 317.75570636221465
_MAIN_CY_PX = 228.0517433641685
_MAIN_CAMERA_ORIGIN = (0.002519, 0.0295, 0.258545)
_METRIC_SAMPLES = 3


class OpenDoorWithVision(Skill):
    """Find and grasp a frontal protruding bar/lever handle, then pull it.

    A tight detection of a known-height, vivid vertical handle provides metric
    monocular range. The head camera positions the base; the wrist camera then
    refines range and alignment. This experimental skill rejects clipped or
    unstable detections, unreachable geometry, excessive approach load, and an
    unverified grasp.
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
    _handle_color = "yellow"
    _handle_height_m = 0.10
    _frame_index = 0

    @resource
    def _proxy(self):
        return gemlib.make_client()

    def _odom_xyt(self):
        pose = self.mobility.odom_xyt(self.odom)
        if pose is None:
            self.fail("Odometry is required for handle acquisition")
        return pose

    def _save_frame(self, image, camera: str, label: str):
        frame_name = f"{self._frame_index:02d}_{camera}_{label}.jpg"
        self._frame_index += 1
        directory = self.debug_directory
        if directory is not None:
            try:
                (directory / frame_name).write_bytes(image.jpeg)
            except Exception as error:  # noqa: BLE001 - observability must not block safety
                self.logger.warning(f"[OpenDoorWithVision] could not save {frame_name}: {error}")
        return frame_name

    def _detect_box(self, image, camera: str):
        frame_name = self._save_frame(image, camera, "detection")
        text = gemlib.ask_image(
            self._proxy,
            image,
            f"Find {self._handle_description!r}: the physical door, drawer, or cabinet "
            "HANDLE the robot should grasp. "
            "Choose a protruding bar or lever handle, not the door panel, hinge, lock, "
            "edge, or an image/reflection of a handle. Return ONLY a JSON list with the "
            'best match first: [{"box_2d":[ymin,xmin,ymax,xmax],'
            '"grasp_point":[y,x]}], normalized 0-1000. Empty list if no graspable '
            f"handle is visible. It is the {self._handle_color} component. "
            "The box must be TIGHT around the complete vivid-colored "
            "vertical handle, including its physical top and bottom.",
            logger=self.logger,
        )
        boxes = vision.parse_det_boxes(text)
        if not boxes:
            self.fail(f"Could not identify a graspable handle in the {camera} camera")
        try:
            box = validate_vertical_box(boxes[0])
        except ValueError as error:
            self.fail(str(error))
        self.debug_event("handle_detection", camera=camera, box=list(box), response=text, frame=frame_name)
        return box

    def _next_image(self, camera, previous, timeout=1.5):
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            image = self.main_image if camera == "head" else self.wrist_image
            if image is not previous:
                return image
            self.sleep(0.04)
        self.fail(f"No fresh {camera} camera frames while measuring the handle")

    def _measure_handle(self, camera: str, focal_y_px: float):
        boxes = []
        previous = None
        for sample in range(_METRIC_SAMPLES):
            image = self.main_image if camera == "head" else self.wrist_image
            if sample:
                image = self._next_image(camera, previous)
            previous = image
            box = self._detect_box(image, camera)
            boxes.append(box)
            self.debug_event(
                "metric_handle_observation",
                camera=camera,
                sample=sample,
                box=list(box),
                source="gemini_tight_box",
            )
        try:
            box = stable_vertical_box(boxes)
        except ValueError as error:
            self.fail(str(error))
        optical_range = focal_y_px * self._handle_height_m / box[3]
        self.debug_event(
            "metric_handle_result",
            camera=camera,
            box=list(box),
            physical_height_m=self._handle_height_m,
            focal_y_px=focal_y_px,
            optical_range_m=optical_range,
        )
        return box, optical_range

    def _rotate(self, radians):
        before = self._odom_xyt()
        if not self.mobility.rotate_by(self._odom_xyt, radians, logger=self.logger):
            self.fail("Base rotation failed during handle acquisition")
        self.debug_event("base_rotation", requested_rad=radians, before=list(before), after=list(self._odom_xyt()))

    def _drive(self, metres):
        before = self._odom_xyt()
        if not self.mobility.drive(self._odom_xyt, metres, logger=self.logger):
            self.fail("Base motion failed during handle acquisition")
        self.debug_event("base_translation", requested_m=metres, before=list(before), after=list(self._odom_xyt()))

    def _localize_handle(self):
        self.sleep(0.8)
        box, _measured_range = self._measure_handle("head", _MAIN_FY_PX)
        try:
            relative, distance = vertical_handle_target(
                box,
                self._handle_height_m,
                fx=_MAIN_FX_PX,
                fy=_MAIN_FY_PX,
                cx=_MAIN_CX_PX,
                cy=_MAIN_CY_PX,
                camera_origin=_MAIN_CAMERA_ORIGIN,
            )
        except ValueError as error:
            self.fail(str(error))
        if not _MIN_RANGE_M <= distance <= _MAX_RANGE_M:
            self.fail(f"Estimated handle range {distance:.2f} m is outside the safe envelope")
        if not _MIN_HANDLE_Z_M <= relative[2] <= _MAX_HANDLE_Z_M:
            self.fail(f"Estimated handle height {relative[2]:.2f} m is outside the arm workspace")
        self.debug_event(
            "handle_size_localized",
            base_point=list(relative),
            box=list(box),
            physical_height_m=self._handle_height_m,
            range_m=distance,
        )
        odom = self._odom_xyt()
        c, s = math.cos(odom[2]), math.sin(odom[2])
        point_odom = (
            odom[0] + c * relative[0] - s * relative[1],
            odom[1] + s * relative[0] + c * relative[1],
            relative[2],
        )
        return point_odom

    def _position_base(self, point):
        relative = odom_point_to_base(point, self._odom_xyt())
        self._rotate(math.atan2(relative[1], relative[0]))
        relative = odom_point_to_base(point, self._odom_xyt())
        self._drive(math.hypot(relative[0], relative[1]) - _APPROACH_RANGE_M)
        final_odom = self._odom_xyt()
        relative = odom_point_to_base(point, final_odom)
        accepted = 0.41 <= relative[0] <= 0.45 and abs(relative[1]) <= 0.05
        self.debug_event(
            "base_position_result",
            odom=list(final_odom),
            handle_base=list(relative),
            accepted=accepted,
            x_bounds_m=[0.41, 0.45],
            max_abs_y_m=0.05,
        )
        if not accepted:
            self.fail("Base could not place the handle inside the arm's safe workspace")
        self.debug_event("base_positioned", handle_base=list(relative))
        return relative

    def _wrist_align(self, target):
        x = _PREGRASP_X_M
        y, z = target[1], target[2]
        self.manipulation.torque_on()
        self.manipulation.gripper_open(duration=1.0)
        self.manipulation.move_to(x, y, z, duration=2.0)
        for step in range(_WRIST_STEPS):
            self.sleep(0.5)
            box, optical_range = self._measure_handle("wrist", _WRIST_FY_PX)
            u, v = box[0] + box[2] / 2.0, box[1] + box[3] / 2.0
            err_u, err_v = u - _WRIST_U, v - _WRIST_V
            range_error = optical_range - _WRIST_STOP_RANGE_M
            self.debug_event(
                "wrist_alignment",
                step=step,
                pixel=[u, v],
                box=list(box),
                optical_range_m=optical_range,
                range_error_m=range_error,
                error=[err_u, err_v],
                pose=[x, y, z],
            )
            if optical_range < _WRIST_STOP_RANGE_M - _WRIST_RANGE_DEADBAND_M:
                self.fail("Wrist camera is already too close to see the complete handle safely")
            centered = abs(err_u) <= _WRIST_DEADBAND_PX and abs(err_v) <= _WRIST_DEADBAND_PX
            if centered and abs(range_error) <= _WRIST_RANGE_DEADBAND_M:
                return x, y, z
            dx = max(0.0, min(_WRIST_RANGE_MAX_STEP_M, range_error))
            dy = max(-_WRIST_MAX_STEP_M, min(_WRIST_MAX_STEP_M, -_WRIST_GAIN_M_PER_100PX * err_u / 100.0))
            dz = max(-_WRIST_MAX_STEP_M, min(_WRIST_MAX_STEP_M, -_WRIST_GAIN_M_PER_100PX * err_v / 100.0))
            x = min(0.40, x + dx)
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
        # fingers just beyond the estimated handle plane, never the wrist.
        final_x = max(x, min(0.40, handle_x - _FINGERTIP_OFFSET_M + _APPROACH_PAST_HANDLE_M))
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
        handle_color: str = "yellow",
        handle_height_m: float = 0.10,
        pull_distance_m: float = 0.05,
    ) -> SkillReturn:
        """Locate, grasp, and pull a frontal protruding handle."""
        if self._proxy is None:
            self.fail("Innate proxy not configured (INNATE_SERVICE_KEY)")
        if not 0.01 <= pull_distance_m <= 0.20:
            self.fail("pull_distance_m must be between 0.01 and 0.20")
        if not handle_description.strip():
            self.fail("handle_description must not be empty")
        if not any(color in handle_color.lower() for color in ("red", "orange", "yellow", "green", "blue", "purple", "pink")):
            self.fail("handle_color must name a vivid red, orange, yellow, green, blue, purple, or pink color")
        if not 0.03 <= handle_height_m <= 0.25:
            self.fail("handle_height_m must be between 0.03 and 0.25")
        self._handle_description = handle_description.strip()
        self._handle_color = handle_color.strip().lower()
        self._handle_height_m = handle_height_m
        self._frame_index = 0
        try:
            self.debug_event(
                "acquisition_started",
                handle_description=self._handle_description,
                handle_color=self._handle_color,
                pull_distance_m=pull_distance_m,
                handle_height_m=handle_height_m,
                localization="known_vertical_size",
            )
            self.head.set_position(int(_HEAD_TILT_DEG))
            self.manipulation.torque_on()
            self.manipulation.gripper_open(duration=1.0)
            self.manipulation.move_joints(_SEARCH_ARM, duration=3.0)
            point = self._localize_handle()
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
