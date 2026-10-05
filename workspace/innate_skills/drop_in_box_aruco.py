# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Rehearsed fixed-box drop with live marker guidance and no Gemini calls."""

import copy
import math
import time

from innate_skills.approach import FloorApproach
from innate_skills.box_marker import MarkerDock, MarkerFollower, load_config, observe_box_release
from innate_skills.box_nav2 import dock_with_arc
from innate_skills.drop_in_box_fast import DropInBoxFast
from innate_skills.sock_grip import fresh_sock_held
from innate_skills.vertical_lift import lift_vertical

from innate import SkillReturn
from innate.exceptions import SkillFailed


class DropInBoxAruco(DropInBoxFast):
    """Drop the sock into the taught marker box; stop if the marker cannot be trusted."""

    DROP_RIGHT_M = 0.08
    FINAL_CLOSER_M = 0.04
    # Rehearsed box: 45 cm across, 15 cm deep, 10–12 cm rim. Leave
    # 3 cm to the far wall for the fingers; these are wrist coordinates.
    _p = {**DropInBoxFast._p, "drop_inset": 0.12}
    RELEASE_Z = 0.26  # cross the rim at clearance height before lowering
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
        # A live target is already 12 cm inside along the box normal. Testing
        # robot-X against the taught edge would reject valid oblique targets.

    def _retract(self):
        if getattr(self, "_vertical_lift_pending", False):
            # A failed lift must not trigger the very sweep we refused.
            self.mobility.stop()
            return
        super()._retract()

    def execute(self) -> SkillReturn:
        self._box_release_xy = None
        config = load_config()  # fail before motion when setup is absent/invalid
        if config["head_tilt_deg"] != self._p["tilt_deg"]:
            raise SkillFailed("Drop head angle changed; reteach the box")
        final_config = copy.deepcopy(config)
        final_config["near_xy"][0] = max(0.19, config["near_xy"][0] - self.FINAL_CLOSER_M)
        final_config["base_from_marker"][0][3] = final_config["near_xy"][0]
        final_dock = MarkerDock(self, final_config)  # validate camera target before motion
        near_x, near_y = final_config["near_xy"]
        # Validate reachability before driving. Measured pose checks also remain
        # inside _release_at; an IK answer alone never authorizes opening.
        x, y = self._release_xy(near_x, near_y)
        if x < near_x + self._p["drop_inset_min"]:
            raise SkillFailed("Taught drop inset is unreachable")
        for px, pz in [
            (self._p["carry_x"], 0.28),
            (x, self.RELEASE_Z),
            (x, self.CLEARANCE_Z),
            (x, self.LOWER_RELEASE_Z),
        ]:
            if not self.manipulation.reachable(px, y, pz, pitch=self._p["arm_pitch"]):
                raise SkillFailed(f"Drop arm waypoint unreachable: x={px:.3f}, y={y:.3f}, z={pz:.3f} m")
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
            lift_vertical(self)
            self.check_cancelled()
            self._over_rim = True
            self._clearance_z = self.CLEARANCE_Z
            self.overlay.begin("marker box", stages=["approach", "release"], frame=tuple(config["image_size"]))
            self.overlay.stage("approach")
            dock_with_arc(self, config, final_distance=near_x)
            if not fresh_sock_held(self):
                raise SkillFailed("Sock slipped during approach; refusing an empty drop")
            final_dock.horizontal_tolerance = 8
            final_dock.size_tolerance = 0.06
            final_dock.search_before_approach = False
            final_dock.approach_timeout = 50
            follower = MarkerFollower(self.mobility)
            follower.max_linear = 0.01
            follower.max_reverse = 0.008
            follower.max_angular = 0.05
            final_dock.run(follower)
            if not fresh_sock_held(self):
                raise SkillFailed("Sock slipped during final alignment; refusing an empty drop")
            release_x, release_y, box_yaw = observe_box_release(
                self, final_dock.detector, self.DROP_RIGHT_M, self._p["drop_inset"], return_heading=True
            )
            if abs(box_yaw) > math.radians(15):
                raise SkillFailed("Box still oblique after Nav2 approach; holding socks")
            self._box_release_xy = (release_x, release_y)
            x, y = self._box_release_xy
            for px, pz in [
                (self._p["carry_x"], 0.28),
                (x, self.RELEASE_Z),
                (x, self.LOWER_RELEASE_Z),
                (x, self.CLEARANCE_Z),
            ]:
                if not self.manipulation.reachable(px, y, pz, pitch=self._p["arm_pitch"]):
                    raise SkillFailed(f"Box-relative drop is unreachable: x={px:.3f}, y={y:.3f}, z={pz:.3f} m")
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
            self.head.set_position(0)
