# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Rehearsed fixed-box drop with live marker guidance and no Gemini calls."""

from innate_skills.approach import FloorApproach
from innate_skills.box_marker import MarkerDock, load_config
from innate_skills.drop_in_box_fast import DropInBoxFast
from innate_skills.sock_grip import fresh_sock_held

from innate import SkillReturn
from innate.exceptions import SkillFailed


class DropInBoxAruco(DropInBoxFast):
    """Drop the sock into the taught marker box; stop if the marker cannot be trusted."""

    def execute(self) -> SkillReturn:
        config = load_config()  # fail before motion when setup is absent/invalid
        if config["head_tilt_deg"] != self._p["tilt_deg"]:
            raise SkillFailed("Drop head angle changed; reteach the box")
        near_x, near_y = config["near_xy"]
        # Validate reachability before driving. Measured pose checks also remain
        # inside _release_at; an IK answer alone never authorizes opening.
        x, y = self.manipulation.clamp_reach(near_x + self._p["drop_inset"], near_y)
        if x < near_x + self._p["drop_inset_min"]:
            raise SkillFailed("Taught drop inset is unreachable")
        for px, pz in [(self._p["carry_x"], 0.28), (x, self.RELEASE_Z), (x, self.CLEARANCE_Z)]:
            if not self.manipulation.reachable(px, y, pz, pitch=self._p["arm_pitch"]):
                raise SkillFailed("Taught overhead arm trajectory is unreachable")
        self._box_u = self._box_top_v = self._near_rim_v = None
        self._rim_z = 0.12
        self._over_rim = self._released = self._fold_started = False
        self._clearance_z = None
        try:
            self.head.set_position(int(round(config["head_tilt_deg"])))
            self.wait_for(lambda: self.joint_states, timeout=3)
            if not self._holding(self._secure_grip()):
                raise SkillFailed("Gripper empty; pick up a sock before dropping")
            self._carry_pose(self._p["travel_joints"])
            self.overlay.begin("marker box", stages=["approach", "release"], frame=tuple(config["image_size"]))
            self.overlay.stage("approach")
            MarkerDock(self, config).run()
            if not fresh_sock_held(self):
                raise SkillFailed("Sock slipped during approach; refusing an empty drop")
            self._release_at(near_x, near_y)
            # _release_at raises the arm and verifies clearance before returning.
            if self._over_rim:
                raise SkillFailed("Arm has not cleared the rim; refusing retreat")
            retreat = FloorApproach(self, {**self._p, "drive_v_max": 0.10, "drive_v_min": 0.04}, self._detect_px)
            if not retreat.drive(-0.15):
                raise SkillFailed("Released at taught pose, but retreat failed")
            # No vision verdict: release at a taught pose is not proof of landing.
            return "Released sock at the taught box pose and raised the arm; landing was not visually verified."
        finally:
            self.mobility.stop()
            self._retract()
            self.head.set_position(0)
