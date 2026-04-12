#!/usr/bin/env python3
"""Tests for arm servo control atomic skills."""

import skills.tests.conftest  # noqa: F401
import unittest
from unittest.mock import MagicMock

from brain_client.skill_types import InterfaceType, SkillResult


class TestArmTorqueOn(unittest.TestCase):
    def setUp(self):
        from skills.arm_torque_on import ArmTorqueOn
        self.skill = ArmTorqueOn(logger=MagicMock())
        mock_manip = MagicMock()
        self.skill.inject_interface(InterfaceType.MANIPULATION, mock_manip)
        self.mock_manip = mock_manip

    def test_execute_calls_torque_on(self):
        self.mock_manip.torque_on.return_value = True
        msg, result = self.skill.execute()
        self.mock_manip.torque_on.assert_called_once()
        self.assertEqual(result, SkillResult.SUCCESS)

    def test_execute_returns_failure(self):
        self.mock_manip.torque_on.return_value = False
        msg, result = self.skill.execute()
        self.assertEqual(result, SkillResult.FAILURE)

    def test_execute_fails_without_interface(self):
        from skills.arm_torque_on import ArmTorqueOn
        skill = ArmTorqueOn(logger=MagicMock())
        msg, result = skill.execute()
        self.assertEqual(result, SkillResult.FAILURE)

    def test_name(self):
        self.assertEqual(self.skill.name, "arm_torque_on")


class TestArmTorqueOff(unittest.TestCase):
    def setUp(self):
        from skills.arm_torque_off import ArmTorqueOff
        self.skill = ArmTorqueOff(logger=MagicMock())
        mock_manip = MagicMock()
        self.skill.inject_interface(InterfaceType.MANIPULATION, mock_manip)
        self.mock_manip = mock_manip

    def test_execute_calls_torque_off(self):
        self.mock_manip.torque_off.return_value = True
        msg, result = self.skill.execute()
        self.mock_manip.torque_off.assert_called_once()
        self.assertEqual(result, SkillResult.SUCCESS)

    def test_execute_returns_failure(self):
        self.mock_manip.torque_off.return_value = False
        msg, result = self.skill.execute()
        self.assertEqual(result, SkillResult.FAILURE)

    def test_execute_fails_without_interface(self):
        from skills.arm_torque_off import ArmTorqueOff
        skill = ArmTorqueOff(logger=MagicMock())
        msg, result = skill.execute()
        self.assertEqual(result, SkillResult.FAILURE)

    def test_name(self):
        self.assertEqual(self.skill.name, "arm_torque_off")


class TestArmRebootServos(unittest.TestCase):
    def setUp(self):
        from skills.arm_reboot_servos import ArmRebootServos
        self.skill = ArmRebootServos(logger=MagicMock())
        mock_manip = MagicMock()
        self.skill.inject_interface(InterfaceType.MANIPULATION, mock_manip)
        self.mock_manip = mock_manip

    def test_execute_calls_reboot(self):
        self.mock_manip.reboot_servos.return_value = True
        msg, result = self.skill.execute()
        self.mock_manip.reboot_servos.assert_called_once()
        self.assertEqual(result, SkillResult.SUCCESS)

    def test_execute_returns_failure(self):
        self.mock_manip.reboot_servos.return_value = False
        msg, result = self.skill.execute()
        self.assertEqual(result, SkillResult.FAILURE)

    def test_execute_fails_without_interface(self):
        from skills.arm_reboot_servos import ArmRebootServos
        skill = ArmRebootServos(logger=MagicMock())
        msg, result = skill.execute()
        self.assertEqual(result, SkillResult.FAILURE)

    def test_name(self):
        self.assertEqual(self.skill.name, "arm_reboot_servos")


if __name__ == "__main__":
    unittest.main()
