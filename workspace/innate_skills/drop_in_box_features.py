# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Feature-guided alternative to the rehearsed ArUco sock-drop skill."""

from innate_skills.approach import FloorApproach
from innate_skills.box_features import FeatureObserver, dock_features, release_target
from innate_skills.drop_in_box_aruco import DropInBoxAruco
from innate_skills.drop_return import retract_to_rest
from innate_skills.sock_grip import fresh_sock_held
from innate_skills.vertical_lift import lift_vertical

from innate import HeadState, SkillReturn
from innate.exceptions import SkillFailed


class DropInBoxFeatures(DropInBoxAruco):
    """Drop a held sock into the learned 50 x 30 x 16 cm wicker basket.

    Start with the basket visible in the main camera and a clear approach path.
    Matches its natural texture, with no marker or language-model localization.
    Stops to observe between short moves; fails holding the sock if no reliable
    pose is available. Experimental: physical docking/drop is not yet validated.
    """

    head_position: HeadState

    # Preserve the rehearsed wrist-to-rim offsets for a rim 4 cm taller.
    RELEASE_Z = 0.36
    LOWER_RELEASE_Z = 0.22
    CLEARANCE_Z = 0.30

    def execute(self) -> SkillReturn:
        self._box_release_xy = None
        self._box_u = self._box_top_v = self._near_rim_v = None
        self._rim_z = 0.16
        self._over_rim = self._released = self._fold_started = False
        self._clearance_z = None
        self._vertical_lift_pending = True
        try:
            observer = FeatureObserver()
            # Validate the taller basket's nominal reach before any motion.
            self._select_drop_pitch(
                self.FINAL_DISTANCE_M + self._p["drop_inset"], -self.DROP_RIGHT_M
            )
            self.mobility.stop()
            self.head.set_position(int(self._p["tilt_deg"]))
            if self.wait_for(lambda: self.joint_states, timeout=3) is None:
                raise SkillFailed("No arm state before feature drop")
            settled = self.wait_for(
                lambda: (
                    True
                    if abs(self.head_position.pitch_degrees - self._p["tilt_deg"]) <= 1
                    else None
                ),
                timeout=3,
            )
            if settled is None:
                raise SkillFailed("Head did not settle before basket observation")
            self.sleep(0.3)
            if not self._holding(self._secure_grip()):
                raise SkillFailed("Gripper empty; pick up a sock before dropping")
            lift_vertical(self, height=None, carry_joints=self._p["travel_joints"])
            self.check_cancelled()
            self._over_rim = True
            self._clearance_z = self.CLEARANCE_Z
            self.overlay.begin(
                "feature basket", stages=["approach", "release"], frame=(1280, 720)
            )
            self.overlay.stage("approach")

            def verify_hold():
                if not fresh_sock_held(self, timeout=None):
                    raise SkillFailed(
                        "Sock slipped during feature approach; refusing an empty drop"
                    )

            # Docking reobserves after the grip check and repeats that check
            # if a new pose requires any further base movement.
            center, normal = dock_features(self, observer, verify_hold=verify_hold)
            self._box_release_xy = release_target(
                center, normal, self._p["drop_inset"], self.DROP_RIGHT_M
            )
            self._select_drop_pitch(*self._box_release_xy)
            self.check_cancelled()
            self._vertical_lift_pending = False
            self._release_at(*center)
            if self._over_rim:
                raise SkillFailed("Arm has not cleared the rim; refusing retreat")
            retreat = FloorApproach(
                self,
                {**self._p, "drive_v_max": 0.20, "drive_v_min": 0.04, "drive_kp": 1.2},
                self._detect_px,
            )
            retract_to_rest(self, block=False)
            self._fold_started = True
            if not retreat.drive(-0.15):
                raise SkillFailed("Sock released at feature target, but retreat failed")
            return "Released sock inside the feature-detected basket and raised the arm; landing was not visually verified."
        finally:
            self.mobility.stop()
            self._retract()
