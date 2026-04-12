"""Tests for the Italian arm wave (che vuoi) skill."""

import skills.tests.conftest  # noqa: F401 — must be imported before skill modules

from unittest.mock import MagicMock, patch

import pytest

from brain_client.skill_types import InterfaceType, SkillResult
from skills.italian_arm_wave import ItalianArmWave


@pytest.fixture
def mock_logger():
    return MagicMock()


@pytest.fixture
def mock_manipulation():
    manip = MagicMock()
    manip.move_to_joint_positions.return_value = True
    return manip


@pytest.fixture
def skill(mock_logger, mock_manipulation):
    s = ItalianArmWave(mock_logger)
    s.manipulation = mock_manipulation
    return s


def _joint_positions_from_call(call):
    if call is None:
        return None
    if call.kwargs and "joint_positions" in call.kwargs:
        return call.kwargs["joint_positions"]
    if call.args:
        return call.args[0]
    return None


class TestSkillMetadata:
    def test_name(self, skill):
        assert skill.name == "italian_arm_wave"

    def test_guidelines_returns_string(self, skill):
        guidelines = skill.guidelines()
        assert isinstance(guidelines, str)
        assert "che vuoi" in guidelines.lower() or "italian" in guidelines.lower()

    def test_requires_manipulation_interface(self, skill):
        interfaces = skill.get_required_interfaces()
        assert InterfaceType.MANIPULATION in interfaces


class TestExecution:
    def test_execute_calls_move_to_joint_positions(self, skill, mock_manipulation):
        with patch("skills.italian_arm_wave.time.sleep"):
            result_msg, result_status = skill.execute()
        assert result_status == SkillResult.SUCCESS
        assert mock_manipulation.move_to_joint_positions.called

    def test_execute_phase_order(self, skill, mock_manipulation):
        with patch("skills.italian_arm_wave.time.sleep"):
            skill.execute(num_bounces=2)

        calls = mock_manipulation.move_to_joint_positions.call_args_list
        assert calls[0].kwargs["duration"] == 2.0
        assert calls[0].kwargs["joint_positions"] == [0.3, -1.2, 1.8, 0.0, 0.5, 0.0]

        # 2 bounces = 4 half-cycles + 1 raise + 1 return = 6 total calls
        assert len(calls) == 6

    def test_execute_returns_success(self, skill):
        with patch("skills.italian_arm_wave.time.sleep"):
            result_msg, result_status = skill.execute()
        assert result_status == SkillResult.SUCCESS
        assert "italian arm wave" in result_msg.lower() or "completed" in result_msg.lower()

    def test_execute_fails_without_manipulation(self, skill):
        skill.manipulation = None
        result_msg, result_status = skill.execute()
        assert result_status == SkillResult.FAILURE

    def test_default_num_bounces(self, skill, mock_manipulation):
        with patch("skills.italian_arm_wave.time.sleep"):
            skill.execute()
        calls = mock_manipulation.move_to_joint_positions.call_args_list
        assert len(calls) == 12


class TestCancellation:
    def test_cancel_during_raise(self, skill, mock_manipulation):
        def cancel_on_first_sleep(_duration):
            skill._cancelled = True

        with patch("skills.italian_arm_wave.time.sleep", side_effect=cancel_on_first_sleep):
            result_msg, result_status = skill.execute()

        assert result_status == SkillResult.CANCELLED

    def test_cancel_during_bounces(self, skill, mock_manipulation):
        call_count = 0

        def cancel_after_3_moves(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                skill._cancelled = True
            return True

        mock_manipulation.move_to_joint_positions.side_effect = cancel_after_3_moves

        with patch("skills.italian_arm_wave.time.sleep"):
            result_msg, result_status = skill.execute()

        assert result_status == SkillResult.CANCELLED

    def test_cancel_method(self, skill):
        result = skill.cancel()
        assert skill._cancelled is True
        assert "cancelled" in result.lower()


class TestParameters:
    def test_intensity_scales_raise_pose(self, skill, mock_manipulation):
        with patch("skills.italian_arm_wave.time.sleep"):
            skill.execute(num_bounces=1, intensity=0.5)

        first_call = mock_manipulation.move_to_joint_positions.call_args_list[0]
        joint_positions = _joint_positions_from_call(first_call)

        expected = [v * 0.5 for v in skill.RAISE_POSE]
        for actual, exp in zip(joint_positions, expected):
            assert abs(actual - exp) < 0.001

    def test_intensity_clamped_to_0_1(self, skill, mock_manipulation):
        with patch("skills.italian_arm_wave.time.sleep"):
            skill.execute(num_bounces=1, intensity=2.0)

        first_call = mock_manipulation.move_to_joint_positions.call_args_list[0]
        joint_positions = _joint_positions_from_call(first_call)

        expected = list(skill.RAISE_POSE)
        for actual, exp in zip(joint_positions, expected):
            assert abs(actual - exp) < 0.001

    def test_custom_rest_pose(self, skill, mock_manipulation):
        custom_rest = [0.1, -0.1, 0.2, 0.0, 0.0, 0.0]
        with patch("skills.italian_arm_wave.time.sleep"):
            skill.execute(num_bounces=1, rest_pose=custom_rest)

        last_call = mock_manipulation.move_to_joint_positions.call_args_list[-1]
        joint_positions = _joint_positions_from_call(last_call)
        assert joint_positions == custom_rest

    def test_num_bounces_zero(self, skill, mock_manipulation):
        with patch("skills.italian_arm_wave.time.sleep"):
            result_msg, result_status = skill.execute(num_bounces=0)

        assert result_status == SkillResult.SUCCESS
        assert len(mock_manipulation.move_to_joint_positions.call_args_list) == 2
