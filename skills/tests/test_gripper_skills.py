#!/usr/bin/env python3
"""Tests for gripper atomic skills."""

import skills.tests.conftest  # noqa: F401
import unittest
from unittest.mock import MagicMock

from brain_client.skill_types import InterfaceType, SkillResult


class TestGripperOpen(unittest.TestCase):
    def setUp(self):
        from skills.gripper_open import GripperOpen
        self.skill = GripperOpen(logger=MagicMock())
        mock_manip = MagicMock()
        self.skill.inject_interface(InterfaceType.MANIPULATION, mock_manip)
        self.mock_manip = mock_manip

    def test_execute_calls_open_gripper_default(self):
        self.mock_manip.open_gripper.return_value = True
        msg, result = self.skill.execute()
        self.mock_manip.open_gripper.assert_called_once_with(percent=100.0, duration=0.5, blocking=True)
        self.assertEqual(result, SkillResult.SUCCESS)

    def test_execute_calls_open_gripper_custom(self):
        self.mock_manip.open_gripper.return_value = True
        msg, result = self.skill.execute(percent=50.0, duration=1.0)
        self.mock_manip.open_gripper.assert_called_once_with(percent=50.0, duration=1.0, blocking=True)
        self.assertEqual(result, SkillResult.SUCCESS)

    def test_execute_returns_failure_on_interface_error(self):
        self.mock_manip.open_gripper.return_value = False
        msg, result = self.skill.execute()
        self.assertEqual(result, SkillResult.FAILURE)

    def test_execute_fails_without_interface(self):
        from skills.gripper_open import GripperOpen
        skill = GripperOpen(logger=MagicMock())
        msg, result = skill.execute()
        self.assertEqual(result, SkillResult.FAILURE)

    def test_name(self):
        self.assertEqual(self.skill.name, "gripper_open")


class TestGripperClose(unittest.TestCase):
    def setUp(self):
        from skills.gripper_close import GripperClose
        self.skill = GripperClose(logger=MagicMock())
        mock_manip = MagicMock()
        self.skill.inject_interface(InterfaceType.MANIPULATION, mock_manip)
        self.mock_manip = mock_manip

    def test_execute_calls_close_gripper_default(self):
        self.mock_manip.close_gripper.return_value = True
        msg, result = self.skill.execute()
        self.mock_manip.close_gripper.assert_called_once_with(strength=0.0, duration=0.5, blocking=True)
        self.assertEqual(result, SkillResult.SUCCESS)

    def test_execute_calls_close_gripper_custom(self):
        self.mock_manip.close_gripper.return_value = True
        msg, result = self.skill.execute(strength=0.1, duration=1.0)
        self.mock_manip.close_gripper.assert_called_once_with(strength=0.1, duration=1.0, blocking=True)
        self.assertEqual(result, SkillResult.SUCCESS)

    def test_execute_returns_failure_on_interface_error(self):
        self.mock_manip.close_gripper.return_value = False
        msg, result = self.skill.execute()
        self.assertEqual(result, SkillResult.FAILURE)

    def test_execute_fails_without_interface(self):
        from skills.gripper_close import GripperClose
        skill = GripperClose(logger=MagicMock())
        msg, result = skill.execute()
        self.assertEqual(result, SkillResult.FAILURE)

    def test_name(self):
        self.assertEqual(self.skill.name, "gripper_close")


if __name__ == "__main__":
    unittest.main()
