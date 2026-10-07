# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Rehearsed fixed-box drop with live marker guidance and no Gemini calls."""

import copy
import math
import time

from innate_skills.approach import FloorApproach
from innate_skills.box_marker import MarkerDock, MarkerFollower, load_config, observe_box_release
from innate_skills.box_nav2 import dock_via_point
from innate_skills.drop_in_box_fast import DropInBoxFast
from innate_skills.drop_return import retract_to_rest
from innate_skills.sock_grip import fresh_sock_held
from innate_skills.vertical_lift import lift_vertical

from innate import MainHighResImage, SkillReturn
from innate.exceptions import SkillFailed


class DropInBoxAruco(DropInBoxFast):
    """Drop the sock into the taught marker box; stop if the marker cannot be trusted."""

    main_highres_image: MainHighResImage | None

    POSE_TOLERANCE_XY = 0.06
    POSE_TOLERANCE_Z = 0.03

    RIGHT_ROLL_JOINT4 = math.radians(60)

    DROP_RIGHT_M = 0.08
    FINAL_DISTANCE_M = 0.16  # base_link forward distance to the tag reference
    # Rehearsed box: 45 cm across, 15 cm deep, 10–12 cm rim. Leave
    # 2 cm to the far wall for the fingers; these are wrist coordinates.
    _p = {**DropInBoxFast._p, "drop_inset": 0.13, "arm_pitch": math.radians(75), "tilt_deg": -20}
    RELEASE_Z = 0.32  # raised side-entry arc, as demonstrated manually
    ENTRY_ARC_RADIANS = 1.0  # about 57 degrees from the right
    LOWER_RELEASE_Z = 0.18  # fingers and hanging socks extend below the wrist

    def _release_xy(self, near_x, near_y):
        if getattr(self, "_box_release_xy", None) is not None:
            return self._box_release_xy
        x, _ = self.manipulation.clamp_reach(near_x + self._p["drop_inset"], near_y)
        # base_link +y is left. Keep the exact lateral target rather than the
        # generic grasp-box clamp (+/-10 cm); execute preflights actual IK.
        return x, near_y - self.DROP_RIGHT_M

    def _require_inset(self, x, near_x):
        if self._box_release_xy is None:
            super()._require_inset(x, near_x)
        # A live target is already 13 cm inside along the box normal. Testing
        # robot-X against the taught edge would reject valid oblique targets.

    def _holding(self, closed):
        return closed and fresh_sock_held(self, timeout=None)

    def _retract(self):
        if getattr(self, "_fold_started", False):
            try:
                self.manipulation.wait()
            finally:
                self._fold_started = False
            return
        if getattr(self, "_vertical_lift_pending", False):
            # A failed lift must not trigger the very sweep we refused.
            self.mobility.stop()
            return
        if not getattr(self, "_released", False):
            super()._retract()
            return
        try:
            if self._over_rim:
                self._lift_out()
            retract_to_rest(self)
        except Exception as e:  # teardown must not mask the skill result
            self.logger.warning(f"[DropInBox] level retract failed; holding arm: {e}")

    def _select_drop_pitch(self, x, y):
        waypoints = [(x, self.RELEASE_Z),
                     (x, self.LOWER_RELEASE_Z), (x, self.CLEARANCE_Z)]
        for degrees in (75, 60, 45, 30):
            pitch = math.radians(degrees)
            reachable = all(self.manipulation.reachable(px, y, z, pitch=pitch) for px, z in waypoints)
            self.logger.info(f"[DropPitch] pitch={degrees} reachable={reachable} target=({x:.3f},{y:.3f})")
            if reachable:
                self._p = {**self._p, "arm_pitch": pitch}
                return
        raise SkillFailed("Drop pose unreachable at pitches 75, 60, 45 and 30 degrees")

    def _reach_over_box(self, x, y):
        # Solve the endpoint first: rotating only joint 1 from the staging
        # pose must land at the measured box-relative position, not a fixed demo pose.
        end = self.manipulation._solve_ik(x, y, self.RELEASE_Z, 0.0, self._p["arm_pitch"], 0.0)
        if end is None:
            raise SkillFailed("Drop arc endpoint is unreachable")
        start = list(end)
        start[0] = max(-math.pi / 2, end[0] - self.ENTRY_ARC_RADIANS)
        current = list(self.joint_states.position[:5])
        staging_s = max(.65, max(abs(a-b) for a,b in zip(start, current, strict=True)) / 2.0)
        self._over_rim = True
        self._clearance_z = self.CLEARANCE_Z
        self.check_cancelled()
        self.manipulation.move_joints(start, duration=staging_s)
        self.check_cancelled()
        sweep_s = max(.65, abs(end[0] - start[0]) / 2.0)
        self.logger.info(
            f"[DropArc] target=({x:.3f},{y:.3f},{self.RELEASE_Z:.3f}) "
            f"joint1={math.degrees(start[0]):.1f}->{math.degrees(end[0]):.1f}deg "
            f"stage_s={staging_s:.3f} sweep_s={sweep_s:.3f}; single endpoint"
        )
        self.manipulation.move_joints(end, duration=sweep_s)
        posed = self.manipulation._settled_pose("drop arc")
        self._require_pose(posed, x, y, self.RELEASE_Z, stage="arc_overhead")
        self.check_cancelled()

    def execute(self) -> SkillReturn:
        self._box_release_xy = None
        config = copy.deepcopy(load_config())
        # Taught pose is base-relative; reproject it for the current head angle.
        config["head_tilt_deg"] = self._p["tilt_deg"]
        final_config = copy.deepcopy(config)
        final_config["near_xy"][0] = self.FINAL_DISTANCE_M
        final_config["base_from_marker"][0][3] = final_config["near_xy"][0]
        final_dock = MarkerDock(self, final_config)  # validate camera target before motion
        near_x, near_y = final_config["near_xy"]
        # Validate reachability before driving. Measured pose checks also remain
        # inside _release_at; an IK answer alone never authorizes opening.
        x, y = self._release_xy(near_x, near_y)
        if x < near_x + self._p["drop_inset_min"]:
            raise SkillFailed("Taught drop inset is unreachable")
        self._select_drop_pitch(x, y)
        self._box_u = self._box_top_v = self._near_rim_v = None
        self._rim_z = 0.12
        self._over_rim = self._released = self._fold_started = False
        self._clearance_z = None
        self._vertical_lift_pending = True
        try:
            self.head.set_position(int(round(config["head_tilt_deg"])))
            self.wait_for(lambda: self.joint_states, timeout=3)
            if not self._holding(self._secure_grip()):
                raise SkillFailed("Gripper empty; pick up a sock before dropping")
            # Lift before any marker search, rotation or approach. Do not
            # move from the folded pose through a low sweeping carry target.
            lift_vertical(self, height=None, carry_joints=self._p["travel_joints"])
            self.check_cancelled()
            self._over_rim = True
            self._clearance_z = self.CLEARANCE_Z
            self.overlay.begin("marker box", stages=["approach", "release"], frame=tuple(config["image_size"]))
            self.overlay.stage("approach")
            dock_via_point(self, config)
            if not fresh_sock_held(self, timeout=None):
                raise SkillFailed("Sock slipped during approach; refusing an empty drop")
            final_dock.search_before_approach = True
            final_dock.approach_timeout = 50
            follower = MarkerFollower(self.mobility)
            follower.final_approach()
            final_dock.run(follower)
            if not fresh_sock_held(self, timeout=None):
                raise SkillFailed("Sock slipped during final alignment; refusing an empty drop")
            release_x, release_y, _box_yaw = observe_box_release(
                self, final_dock.detector, self.DROP_RIGHT_M, self._p["drop_inset"],
                return_heading=True, recover_right=True
            )
            self._box_release_xy = (release_x, release_y)
            x, y = self._box_release_xy
            self._select_drop_pitch(x, y)
            # Until release begins, a failed search/alignment holds the raised
            # arm instead of automatically folding it back toward the box.
            self._vertical_lift_pending = False
            released_at = time.monotonic()
            self._release_at(near_x, near_y)
            self.logger.info(f"[DropTiming] arm release/shake/clear: {time.monotonic() - released_at:.2f}s")
            # _release_at raises the arm and verifies clearance before returning.
            if self._over_rim:
                raise SkillFailed("Arm has not cleared the rim; refusing retreat")
            retreat = FloorApproach(
                self, {**self._p, "drive_v_max": 0.20, "drive_v_min": 0.04, "drive_kp": 1.2}, self._detect_px
            )
            retract_to_rest(self, block=False)
            self._fold_started = True
            retreat_at = time.monotonic()
            if not retreat.drive(-0.15):
                raise SkillFailed("Released at taught pose, but retreat failed")
            self.logger.info(f"[DropTiming] retreat: {time.monotonic() - retreat_at:.2f}s")
            # No vision verdict: release at a taught pose is not proof of landing.
            return "Released sock at the taught box pose and raised the arm; landing was not visually verified."
        finally:
            self.mobility.stop()
            fold_at = time.monotonic()
            self._retract()
            self.logger.info(f"[DropTiming] retract: {time.monotonic() - fold_at:.2f}s")
