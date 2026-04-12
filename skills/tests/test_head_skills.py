#!/usr/bin/env python3
"""Tests for head atomic skills."""

import skills.tests.conftest  # noqa: F401
import unittest
from unittest.mock import MagicMock

from brain_client.skill_types import InterfaceType, SkillResult


class TestHeadSetAngle(unittest.TestCase):
    def setUp(self):
        from skills.head_set_angle import HeadSetAngle
        self.skill = HeadSetAngle(logger=MagicMock())
        mock_head = MagicMock()
        self.skill.inject_interface(InterfaceType.HEAD, mock_head)
        self.mock_head = mock_head

    def test_execute_calls_set_position(self):
        msg, result = self.skill.execute(angle_degrees=-10)
        self.mock_head.set_position.assert_called_once_with(-10)
        self.assertEqual(result, SkillResult.SUCCESS)

    def test_execute_clamps_to_valid_range(self):
        msg, result = self.skill.execute(angle_degrees=-50)
        self.mock_head.set_position.assert_called_once_with(-25)
        self.assertEqual(result, SkillResult.SUCCESS)

        self.mock_head.reset_mock()
        msg, result = self.skill.execute(angle_degrees=30)
        self.mock_head.set_position.assert_called_once_with(15)
        self.assertEqual(result, SkillResult.SUCCESS)

    def test_execute_fails_without_interface(self):
        from skills.head_set_angle import HeadSetAngle
        skill = HeadSetAngle(logger=MagicMock())
        msg, result = skill.execute(angle_degrees=0)
        self.assertEqual(result, SkillResult.FAILURE)

    def test_name(self):
        self.assertEqual(self.skill.name, "head_set_angle")


if __name__ == "__main__":
    unittest.main()
