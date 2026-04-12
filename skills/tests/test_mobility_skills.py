#!/usr/bin/env python3
"""Tests for mobility atomic skills."""

import skills.tests.conftest  # noqa: F401 — must be imported before skill modules
import time
import unittest
from unittest.mock import MagicMock, patch

from brain_client.skill_types import SkillResult


class TestDriveVelocity(unittest.TestCase):
    def setUp(self):
        from skills.drive_velocity import DriveVelocity
        self.skill = DriveVelocity(logger=MagicMock())
        mock_mobility = MagicMock()
        self.skill.inject_interface(
            __import__("brain_client.skill_types", fromlist=["InterfaceType"]).InterfaceType.MOBILITY,
            mock_mobility,
        )
        self.mock_mobility = mock_mobility

    @patch("time.sleep")
    def test_execute_calls_send_cmd_vel(self, mock_sleep):
        msg, result = self.skill.execute(linear_x=0.5, angular_z=0.1, duration=2.0)
        self.mock_mobility.send_cmd_vel.assert_called_once_with(
            linear_x=0.5, angular_z=0.1, duration=2.0
        )
        self.assertEqual(result, SkillResult.SUCCESS)

    def test_execute_fails_without_interface(self):
        from skills.drive_velocity import DriveVelocity
        skill = DriveVelocity(logger=MagicMock())
        msg, result = skill.execute(linear_x=0.5, angular_z=0.0, duration=1.0)
        self.assertEqual(result, SkillResult.FAILURE)

    def test_name(self):
        self.assertEqual(self.skill.name, "drive_velocity")

    def test_guidelines_not_empty(self):
        self.assertIsNotNone(self.skill.guidelines())
        self.assertGreater(len(self.skill.guidelines()), 0)


class TestRotateInPlace(unittest.TestCase):
    def setUp(self):
        from skills.rotate_in_place import RotateInPlace
        self.skill = RotateInPlace(logger=MagicMock())
        mock_mobility = MagicMock()
        self.skill.inject_interface(
            __import__("brain_client.skill_types", fromlist=["InterfaceType"]).InterfaceType.MOBILITY,
            mock_mobility,
        )
        self.mock_mobility = mock_mobility

    @patch("time.sleep")
    def test_execute_calls_rotate_in_place(self, mock_sleep):
        msg, result = self.skill.execute(angular_speed=1.0, duration=3.0)
        self.mock_mobility.rotate_in_place.assert_called_once_with(
            angular_speed=1.0, duration=3.0
        )
        self.assertEqual(result, SkillResult.SUCCESS)

    def test_execute_fails_without_interface(self):
        from skills.rotate_in_place import RotateInPlace
        skill = RotateInPlace(logger=MagicMock())
        msg, result = skill.execute(angular_speed=1.0, duration=1.0)
        self.assertEqual(result, SkillResult.FAILURE)

    def test_name(self):
        self.assertEqual(self.skill.name, "rotate_in_place")


class TestRotateAngle(unittest.TestCase):
    def setUp(self):
        from skills.rotate_angle import RotateAngle
        self.skill = RotateAngle(logger=MagicMock())
        mock_mobility = MagicMock()
        self.skill.inject_interface(
            __import__("brain_client.skill_types", fromlist=["InterfaceType"]).InterfaceType.MOBILITY,
            mock_mobility,
        )
        self.mock_mobility = mock_mobility

    def test_execute_calls_rotate(self):
        self.mock_mobility.rotate.return_value = True
        msg, result = self.skill.execute(angle_radians=1.57)
        self.mock_mobility.rotate.assert_called_once_with(1.57)
        self.assertEqual(result, SkillResult.SUCCESS)

    def test_execute_returns_failure_when_rotate_fails(self):
        self.mock_mobility.rotate.return_value = False
        msg, result = self.skill.execute(angle_radians=1.57)
        self.assertEqual(result, SkillResult.FAILURE)

    def test_execute_fails_without_interface(self):
        from skills.rotate_angle import RotateAngle
        skill = RotateAngle(logger=MagicMock())
        msg, result = skill.execute(angle_radians=1.0)
        self.assertEqual(result, SkillResult.FAILURE)

    def test_name(self):
        self.assertEqual(self.skill.name, "rotate_angle")


if __name__ == "__main__":
    unittest.main()
