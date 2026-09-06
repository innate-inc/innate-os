"""Throw safety and release timing using the real ROS-backed SDK types."""

import math
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from brain_client.robot.exceptions import ArmFailed
from brain_client.robot.manipulation import Manipulation, Waypoint
from brain_client.skills.types import SkillCancelled, SkillFailed

sys.path.insert(0, str(Path(__file__).resolve().parents[5] / "workspace"))
from innate_skills.throw_object import ThrowObject  # noqa: E402


class ThrowTests(unittest.TestCase):
    def skill(self, j6=0.12):
        skill = ThrowObject.__new__(ThrowObject)
        skill.joint_states = SimpleNamespace(position=[0] * 5 + [j6])
        skill.mobility = SimpleNamespace(stop=Mock())
        skill.manipulation = SimpleNamespace(
            GRIPPER_OPEN=Manipulation.GRIPPER_OPEN,
            GRIPPER_MAX_STRENGTH=Manipulation.GRIPPER_MAX_STRENGTH,
            torque_on=Mock(),
            gripper_close=Mock(),
            move_to=Mock(),
            follow=Mock(),
        )
        skill.sleep = lambda _: skill.check_cancelled()
        return skill

    def test_empty_invalid_or_cancelled_grasp_never_releases(self):
        for j6 in [-0.085, Manipulation.GRIPPER_OPEN, math.nan]:
            skill = self.skill(j6)
            with self.assertRaises(SkillFailed):
                skill.execute()
            skill.manipulation.follow.assert_not_called()
            self.assertEqual(skill.mobility.stop.call_count, 2)
        skill = self.skill()
        skill.manipulation.move_to.side_effect = lambda *a, **k: skill._cancel_latch().set()
        with self.assertRaises(SkillCancelled):
            skill.execute()
        skill.manipulation.follow.assert_not_called()
        self.assertEqual(skill.mobility.stop.call_count, 2)
        skill = self.skill()
        skill.manipulation.move_to.side_effect = lambda *a, **k: setattr(
            skill.joint_states, "position", [0] * 5 + [-0.085]
        )
        with self.assertRaises(SkillFailed):
            skill.execute()
        skill.manipulation.follow.assert_not_called()

    def test_swing_is_one_committed_command_and_failure_stops_base(self):
        skill = self.skill()
        # A cancel during the already-committed release must finish the swing.
        skill.manipulation.follow.side_effect = lambda _: skill._cancel_latch().set()
        self.assertIn("Check where it landed", skill.execute())
        skill.manipulation.follow.assert_called_once()
        skill = self.skill()
        skill.manipulation.follow.side_effect = ArmFailed("trajectory rejected")
        with self.assertRaisesRegex(SkillFailed, "trajectory rejected"):
            skill.execute()
        self.assertEqual(skill.mobility.stop.call_count, 2)

    def test_release_is_serialized_in_trajectory_and_carries_forward(self):
        arm = Manipulation.__new__(Manipulation)
        arm._grip_target = -0.4
        arm._solve_ik = Mock(return_value=[0.0] * 5)
        arm.safety = SimpleNamespace(max_ee_speed=None)
        arm.stream_stop = Mock()
        arm._torque_enabled = None
        arm._status_torque = None
        arm._torque_stamp = arm._status_stamp = 0.0
        arm._settled_pose = Mock()
        arm._await_motion_result = Mock(return_value=True)
        arm._goto_js_traj_client = Mock()
        arm._goto_js_traj_client.service_is_ready.return_value = True
        arm.follow([Waypoint(0.2, 0, 0.2), Waypoint(0.3, 0, 0.2, grip=0.85), Waypoint(0.3, 0, 0.3)])
        request = arm._goto_js_traj_client.call_async.call_args.args[0]
        self.assertEqual(request.num_joints, 6)
        self.assertEqual(list(request.waypoints.data)[5::6], [-0.4, 0.85, 0.85])
        self.assertEqual(arm._grip_target, 0.85)
        arm._goto_js_traj_client.call_async.reset_mock()
        with self.assertRaises(ArmFailed):
            arm.follow([Waypoint(0.2, 0, 0.2, grip=math.nan)])
        arm._goto_js_traj_client.call_async.assert_not_called()


if __name__ == "__main__":
    unittest.main()
