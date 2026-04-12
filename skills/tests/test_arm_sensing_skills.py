#!/usr/bin/env python3
"""Tests for arm sensing atomic skills."""

import skills.tests.conftest  # noqa: F401
import unittest
from unittest.mock import MagicMock

from brain_client.skill_types import InterfaceType, SkillResult


class TestArmGetPose(unittest.TestCase):
    def setUp(self):
        from skills.arm_get_pose import ArmGetPose
        self.skill = ArmGetPose(logger=MagicMock())
        mock_manip = MagicMock()
        self.skill.inject_interface(InterfaceType.MANIPULATION, mock_manip)
        self.mock_manip = mock_manip

    def test_execute_returns_pose(self):
        self.mock_manip.get_current_end_effector_pose.return_value = {
            "position": {"x": 0.2, "y": 0.0, "z": 0.3},
            "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
            "frame_id": "base_link",
        }
        msg, result = self.skill.execute()
        self.mock_manip.get_current_end_effector_pose.assert_called_once()
        self.assertEqual(result, SkillResult.SUCCESS)
        self.assertIn("0.2", msg)

    def test_execute_returns_failure_when_no_pose(self):
        self.mock_manip.get_current_end_effector_pose.return_value = None
        msg, result = self.skill.execute()
        self.assertEqual(result, SkillResult.FAILURE)

    def test_execute_fails_without_interface(self):
        from skills.arm_get_pose import ArmGetPose
        skill = ArmGetPose(logger=MagicMock())
        msg, result = skill.execute()
        self.assertEqual(result, SkillResult.FAILURE)

    def test_name(self):
        self.assertEqual(self.skill.name, "arm_get_pose")


class TestArmGetLoad(unittest.TestCase):
    def setUp(self):
        from skills.arm_get_load import ArmGetLoad
        self.skill = ArmGetLoad(logger=MagicMock())
        mock_manip = MagicMock()
        self.skill.inject_interface(InterfaceType.MANIPULATION, mock_manip)
        self.mock_manip = mock_manip

    def test_execute_returns_load(self):
        self.mock_manip.get_motor_load.return_value = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
        msg, result = self.skill.execute()
        self.mock_manip.get_motor_load.assert_called_once()
        self.assertEqual(result, SkillResult.SUCCESS)
        self.assertIn("1.0", msg)

    def test_execute_returns_failure_when_no_load(self):
        self.mock_manip.get_motor_load.return_value = None
        msg, result = self.skill.execute()
        self.assertEqual(result, SkillResult.FAILURE)

    def test_execute_fails_without_interface(self):
        from skills.arm_get_load import ArmGetLoad
        skill = ArmGetLoad(logger=MagicMock())
        msg, result = skill.execute()
        self.assertEqual(result, SkillResult.FAILURE)

    def test_name(self):
        self.assertEqual(self.skill.name, "arm_get_load")


if __name__ == "__main__":
    unittest.main()
