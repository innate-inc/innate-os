# SPDX-License-Identifier: Apache-2.0
"""Read the demonstrated phases, adapt to the live battery, and present it forward."""

from innate_skills.imitate_pick_and_present import ImitatePickAndPresent

from innate import SkillOutput
from innate.gesture import Gesture
from innate.gesture_agent import DemonstrationAgentPolicy


class PickAndHandBatteryAgent(ImitatePickAndPresent):
    """Use Astra to inspect a recorded pickup, infer task phases, and adapt each
    bounded action from live head/wrist views. Picks a battery and presents it
    toward the other robot, keeping the gripper closed. Start with an OPEN EMPTY
    gripper, stationary base, healthy arm and clear supervised workspace. This
    is a new attempt, not a resume of an already-held battery. Does not operate
    the receiving robot or release the battery. Stops on arm faults; no auto reboot.
    """

    decision_timeout = 175
    grip_strength = 0.5

    def make_demo(self, demonstration, legacy_urdf):
        return Gesture(demonstration, image_time_reference=True)

    def make_policy(self, demo):
        return DemonstrationAgentPolicy(demo)

    def execute(
        self,
        demonstration: str = "/home/jetson1/innate-os/workspace/custom_skills/pick-and-hand-battery/raw_data/episode_0.h5",
        object_description: str = "the black battery; present it toward the other robot as demonstrated",
    ) -> SkillOutput:
        if self.manipulation.pose.gripper < 0.7:
            self.fail("Start a new attempt with an open empty gripper; do not launch while holding the battery")
        return super().execute(demonstration, object_description)
