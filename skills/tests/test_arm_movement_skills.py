#!/usr/bin/env python3
"""Tests for arm movement atomic skills."""

import skills.tests.conftest  # noqa: F401
import unittest
from unittest.mock import MagicMock, patch

from brain_client.skill_types import InterfaceType, SkillResult


class TestArmMoveToJoints(unittest.TestCase):
    def setUp(self):
        from skills.arm_move_to_joints import ArmMoveToJoints
        self.skill = ArmMoveToJoints(logger=MagicMock())
        mock_manip = MagicMock()
        self.skill.inject_interface(InterfaceType.MANIPULATION, mock_manip)
        self.mock_manip = mock_manip

    @patch("time.sleep")
    def test_execute_calls_move_to_joint_positions(self, mock_sleep):
        self.mock_manip.move_to_joint_positions.return_value = True
        joints = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
        msg, result = self.skill.execute(joint_positions=joints, duration=3)
        self.mock_manip.move_to_joint_positions.assert_called_once_with(
            joint_positions=joints, duration=3, blocking=False
        )
        self.assertEqual(result, SkillResult.SUCCESS)

    def test_execute_returns_failure_on_interface_error(self):
        self.mock_manip.move_to_joint_positions.return_value = False
        joints = [0.0] * 6
        msg, result = self.skill.execute(joint_positions=joints)
        self.assertEqual(result, SkillResult.FAILURE)

    def test_execute_rejects_wrong_joint_count(self):
        joints = [0.0, 0.0, 0.0]
        msg, result = self.skill.execute(joint_positions=joints)
        self.assertEqual(result, SkillResult.FAILURE)
        self.mock_manip.move_to_joint_positions.assert_not_called()

    def test_execute_fails_without_interface(self):
        from skills.arm_move_to_joints import ArmMoveToJoints
        skill = ArmMoveToJoints(logger=MagicMock())
        msg, result = skill.execute(joint_positions=[0.0] * 6)
        self.assertEqual(result, SkillResult.FAILURE)

    def test_name(self):
        self.assertEqual(self.skill.name, "arm_move_to_joints")


class TestArmFollowTrajectory(unittest.TestCase):
    def setUp(self):
        from skills.arm_follow_trajectory import ArmFollowTrajectory
        self.skill = ArmFollowTrajectory(logger=MagicMock())
        mock_manip = MagicMock()
        self.skill.inject_interface(InterfaceType.MANIPULATION, mock_manip)
        self.mock_manip = mock_manip

    def test_execute_calls_move_cartesian_trajectory(self):
        self.mock_manip.move_cartesian_trajectory.return_value = True
        poses = [
            {"x": 0.2, "y": 0.0, "z": 0.3, "roll": 0.0, "pitch": 0.0, "yaw": 0.0},
            {"x": 0.3, "y": 0.0, "z": 0.3, "roll": 0.0, "pitch": 0.0, "yaw": 0.0},
        ]
        msg, result = self.skill.execute(poses=poses, segment_duration=1.0)
        self.mock_manip.move_cartesian_trajectory.assert_called_once_with(
            poses=poses, segment_duration=1.0
        )
        self.assertEqual(result, SkillResult.SUCCESS)

    def test_execute_returns_failure_on_interface_error(self):
        self.mock_manip.move_cartesian_trajectory.return_value = False
        poses = [
            {"x": 0.2, "y": 0.0, "z": 0.3, "roll": 0.0, "pitch": 0.0, "yaw": 0.0},
            {"x": 0.3, "y": 0.0, "z": 0.3, "roll": 0.0, "pitch": 0.0, "yaw": 0.0},
        ]
        msg, result = self.skill.execute(poses=poses)
        self.assertEqual(result, SkillResult.FAILURE)

    def test_execute_rejects_fewer_than_2_poses(self):
        poses = [{"x": 0.2, "y": 0.0, "z": 0.3, "roll": 0.0, "pitch": 0.0, "yaw": 0.0}]
        msg, result = self.skill.execute(poses=poses)
        self.assertEqual(result, SkillResult.FAILURE)
        self.mock_manip.move_cartesian_trajectory.assert_not_called()

    def test_execute_fails_without_interface(self):
        from skills.arm_follow_trajectory import ArmFollowTrajectory
        skill = ArmFollowTrajectory(logger=MagicMock())
        poses = [
            {"x": 0.2, "y": 0.0, "z": 0.3, "roll": 0.0, "pitch": 0.0, "yaw": 0.0},
            {"x": 0.3, "y": 0.0, "z": 0.3, "roll": 0.0, "pitch": 0.0, "yaw": 0.0},
        ]
        msg, result = skill.execute(poses=poses)
        self.assertEqual(result, SkillResult.FAILURE)

    def test_name(self):
        self.assertEqual(self.skill.name, "arm_follow_trajectory")


if __name__ == "__main__":
    unittest.main()
