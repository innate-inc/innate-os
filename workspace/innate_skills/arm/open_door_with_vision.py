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
    camera_pose_from_ee,
    odom_point_to_base,
    stable_vertical_box,
    validate_vertical_box,
    vertical_handle_from_camera_box,
    vertical_handle_target,
)

_HEAD_TILT_DEG = 0.0
_MIN_RANGE_M = 0.20
_MAX_RANGE_M = 1.50
_MIN_HANDLE_Z_M = 0.10
_MAX_HANDLE_Z_M = 0.30
_APPROACH_RANGE_M = 0.37
_BASE_HANDLE_X_BOUNDS_M = (0.35, 0.395)
_WRIST_STAGING_X_M = 0.32
_VISUAL_CLEARANCE_M = 0.03
_WRIST_DEPTH_CORRECTION_M = 0.015
_MAX_GRASP_HANDLE_X_M = 0.39
_MAX_EE_X_M = 0.40
_APPROACH_PAST_HANDLE_M = 0.01
_WRIST_MAX_STEP_M = 0.025
_WRIST_STEPS = 5
_WRIST_U_PX = 320.0
_WRIST_U_DEADBAND_PX = 10.0
# The real wrist module has no CameraInfo calibration. Keep this aligned with
# the repository's 80-degree wrist-camera model until per-robot K is measured.
_WRIST_FY_PX = 480.0 / (2.0 * math.tan(math.radians(80.0) / 2.0))
_WRIST_FX_PX = _WRIST_FY_PX
_WRIST_CX_PX = 320.0
_WRIST_CY_PX = 240.0
_WRIST_XZ_DEADBAND_M = 0.008
# URDF camera transform expressed relative to ee_link. Both fixed joints are
# defined from link5, so the camera sits 58 mm behind and 51 mm above the EE.
_WRIST_CAMERA_IN_EE = (-0.058058, 0.0, 0.05052)
_WRIST_CAMERA_RPY_IN_EE = (0.0, 0.43633, 0.0)
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
_HEAD_METRIC_SAMPLES = 3
_GRASP_ATTEMPTS = 2
_VISION_REASONING_EFFORT = "low"


def _fuse_handle_target(head_target, wrist_target):
    """Use wrist Y/Z while bounding its uncalibrated monocular depth."""
    wrist_x = max(
        head_target[0] - _WRIST_DEPTH_CORRECTION_M,
        min(head_target[0] + _WRIST_DEPTH_CORRECTION_M, wrist_target[0]),
    )
    return min(_MAX_GRASP_HANDLE_X_M, wrist_x), wrist_target[1], wrist_target[2]


def _wrist_lateral_correction(u_px, optical_range_m):
    """Image-right handle error requires a negative base-Y gripper move."""
    correction = -(u_px - _WRIST_U_PX) * optical_range_m / _WRIST_FX_PX
    return max(-_WRIST_MAX_STEP_M, min(_WRIST_MAX_STEP_M, correction))


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
            reasoning_effort=_VISION_REASONING_EFFORT,
        )
        self.debug_event(
            "handle_detection_response",
            camera=camera,
            response=text,
            frame=frame_name,
        )
        boxes = vision.parse_det_boxes(text)
        self.debug_event(
            "handle_detection_parse",
            camera=camera,
            boxes=[list(box) for box in boxes],
            frame=frame_name,
        )
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
        samples = _HEAD_METRIC_SAMPLES if camera == "head" else 1
        for sample in range(samples):
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
        if samples == 1:
            box = boxes[0]
        else:
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
        accepted = _BASE_HANDLE_X_BOUNDS_M[0] <= relative[0] <= _BASE_HANDLE_X_BOUNDS_M[1] and abs(relative[1]) <= 0.05
        self.debug_event(
            "base_position_result",
            odom=list(final_odom),
            handle_base=list(relative),
            accepted=accepted,
            x_bounds_m=list(_BASE_HANDLE_X_BOUNDS_M),
            max_abs_y_m=0.05,
        )
        if not accepted:
            self.fail("Base could not place the handle inside the arm's safe workspace")
        self.debug_event("base_positioned", handle_base=list(relative))
        return relative

    def _wrist_align(self, target, *, restage=True):
        if restage:
            self.manipulation.torque_on()
            self.manipulation.gripper_open(duration=1.0)
            self.manipulation.move_to(_WRIST_STAGING_X_M, target[1], target[2], duration=2.0)
        for step in range(_WRIST_STEPS):
            self.sleep(0.5)
            box, approximate_range = self._measure_handle("wrist", _WRIST_FY_PX)
            measured = self.manipulation.pose
            camera_origin, camera_rotation = camera_pose_from_ee(
                measured.position,
                measured.rpy,
                translation_in_ee=_WRIST_CAMERA_IN_EE,
                rpy_in_ee=_WRIST_CAMERA_RPY_IN_EE,
            )
            try:
                wrist_observed, residual, top_range, bottom_range = vertical_handle_from_camera_box(
                    box,
                    self._handle_height_m,
                    fx=_WRIST_FX_PX,
                    fy=_WRIST_FY_PX,
                    cx=_WRIST_CX_PX,
                    cy=_WRIST_CY_PX,
                    camera_origin=camera_origin,
                    camera_rotation=camera_rotation,
                )
            except ValueError as error:
                self.fail(str(error))
            observed = _fuse_handle_target(target, wrist_observed)
            u = box[0] + box[2] / 2.0
            u_error = u - _WRIST_U_PX
            lateral_correction = _wrist_lateral_correction(u, approximate_range)
            desired = (
                observed[0] - _VISUAL_CLEARANCE_M,
                measured.y + lateral_correction,
                observed[2],
            )
            error = tuple(desired[index] - measured.position[index] for index in range(3))
            self.debug_event(
                "wrist_alignment",
                step=step,
                box=list(box),
                approximate_optical_range_m=approximate_range,
                camera_origin=list(camera_origin),
                wrist_handle_base_raw=list(wrist_observed),
                fused_handle_base=list(observed),
                head_handle_base=list(target),
                handle_u_px=u,
                handle_u_error_px=u_error,
                lateral_correction_m=lateral_correction,
                endpoint_residual_m=residual,
                endpoint_ray_ranges_m=[top_range, bottom_range],
                measured_ee=list(measured.position),
                desired_ee=list(desired),
                error_m=list(error),
            )
            aligned = (
                abs(error[0]) <= _WRIST_XZ_DEADBAND_M
                and abs(error[2]) <= _WRIST_XZ_DEADBAND_M
                and abs(u_error) <= _WRIST_U_DEADBAND_PX
            )
            if aligned:
                self.debug_event(
                    "wrist_alignment_complete",
                    measured_ee=list(measured.position),
                    refined_handle_base=list(observed),
                )
                return tuple(measured.position), observed

            def bounded(value):
                return max(-_WRIST_MAX_STEP_M, min(_WRIST_MAX_STEP_M, value))

            x = max(_WRIST_STAGING_X_M, min(0.40, measured.x + bounded(error[0])))
            y = max(-0.10, min(0.10, measured.y + bounded(error[1])))
            z = max(_MIN_HANDLE_Z_M, min(_MAX_HANDLE_Z_M, measured.z + bounded(error[2])))
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

    def _grasp(self, pregrasp, handle_x, attempt):
        baseline_samples = []
        for _ in range(_TARE_SAMPLES):
            baseline_samples.append(self._effort())
            self.sleep(0.04)
        baseline = tuple(statistics.median(sample[j] for sample in baseline_samples) for j in range(5))
        x, y, z = pregrasp
        # ee_link is already at the fingertip plane: the finger joint's 44 mm
        # offset plus its 47.7 mm pad matches ee_link's 91.838 mm URDF offset.
        final_x = handle_x + _APPROACH_PAST_HANDLE_M
        if final_x > _MAX_EE_X_M + 1e-6:
            self.fail("Refined handle depth is beyond the arm's verified grasp reach")
        final_x = max(x, final_x)
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
        j6_held = j6 is not None and j6 > _EMPTY_GRIPPER_J6 + 0.02
        image = self.wrist_image
        frame_name = self._save_frame(image, "wrist", f"grasp_verification_{attempt}")
        verdict = (
            gemlib.ask_image(
                self._proxy,
                image,
                f"The robot just closed its two black gripper fingers on {self._handle_description!r}. "
                f"Is the {self._handle_color} physical handle visibly trapped BETWEEN the two fingers? "
                "Answer only YES or NO.",
                logger=self.logger,
                reasoning_effort=_VISION_REASONING_EFFORT,
            )
            if j6_held
            else "SKIPPED: claw position proves an empty close"
        )
        normalized = (verdict or "").strip().upper()
        visual_held = normalized.startswith("YES") and not normalized.startswith("NO")
        held = j6_held and visual_held
        self.debug_event(
            "grasp_verified",
            attempt=attempt,
            gripper_position=j6,
            j6_held=j6_held,
            visual_response=verdict,
            visual_held=visual_held,
            frame=frame_name,
            held=held,
        )
        return held

    def _prepare_grasp_retry(self, target, attempt):
        self.manipulation.gripper_open(duration=1.0)
        measured = self.manipulation.pose
        retreat_x = max(_WRIST_STAGING_X_M, min(measured.x, target[0] - _VISUAL_CLEARANCE_M))
        self.manipulation.move_to(retreat_x, measured.y, measured.z, duration=0.8)
        self.debug_event(
            "grasp_retry_started",
            attempt=attempt,
            retreat_pose=[retreat_x, measured.y, measured.z],
            reason="verified_miss",
        )
        return self._wrist_align(target, restage=False)

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
        if not any(
            color in handle_color.lower() for color in ("red", "orange", "yellow", "green", "blue", "purple", "pink")
        ):
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
            pregrasp, refined_target = self._wrist_align(target)
            for attempt in range(1, _GRASP_ATTEMPTS + 1):
                if self._grasp(pregrasp, refined_target[0], attempt):
                    break
                if attempt == _GRASP_ATTEMPTS:
                    self.fail(f"The gripper missed the handle after {_GRASP_ATTEMPTS} verified attempts")
                pregrasp, refined_target = self._prepare_grasp_retry(target, attempt + 1)
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
