#!/usr/bin/env python3
"""
Italian Arm Wave Skill — the classic "che vuoi" gesture.

Moves the 6-DOF arm through a dramatic raise, theatrical hold,
rhythmic wrist bounces with shoulder bob, and smooth return to rest.
"""

import time

from brain_client.skill_types import Interface, InterfaceType, Skill, SkillResult


class ItalianArmWave(Skill):
    """Perform the Italian 'che vuoi' (what do you want?) gesture."""

    manipulation = Interface(InterfaceType.MANIPULATION)

    # -- Pose constants (radians) --
    # [base_yaw, shoulder_pitch, elbow_pitch, wrist_roll, wrist_pitch, ee_roll]
    RAISE_POSE = [0.3, -1.2, 1.8, 0.0, 0.5, 0.0]

    BOUNCE_UP_DELTAS = [0.0, 0.1, 0.0, 0.0, 0.0, 0.2]
    BOUNCE_DOWN_DELTAS = [0.0, -0.1, 0.0, 0.0, -0.8, -0.2]

    DEFAULT_REST_POSE = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

    # -- Timing constants (seconds) --
    RAISE_DURATION = 2.0
    HOLD_DURATION = 0.5
    BOUNCE_HALF_CYCLE = 0.3
    RETURN_DURATION = 2.0

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False

    @property
    def name(self):
        return "italian_arm_wave"

    def guidelines(self):
        return (
            "Perform the Italian 'che vuoi' gesture — a dramatic arm raise followed by "
            "rhythmic wrist bounces. Use when the robot wants to express confusion, "
            "ask 'what do you want?', or add Italian flair. "
            "Parameters: num_bounces (default 5), intensity (0.0-1.0, default 1.0), "
            "rest_pose (6 joint angles to return to, default all zeros)."
        )

    def execute(self, num_bounces: int = 5, intensity: float = 1.0, rest_pose: list[float] | None = None):
        """
        Perform the Italian 'che vuoi' gesture.

        Args:
            num_bounces: Number of wrist bounce cycles (default 5)
            intensity: Scale factor 0.0-1.0 for motion amplitude (default 1.0)
            rest_pose: Joint positions to return to (default [0,0,0,0,0,0])
        """
        self._cancelled = False
        rest = rest_pose if rest_pose is not None else list(self.DEFAULT_REST_POSE)

        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE

        intensity = max(0.0, min(1.0, intensity))

        # Phase 1: Dramatic raise
        raise_pose = self._scale_pose(self.RAISE_POSE, intensity)
        self.logger.info(f"Phase 1: Raising arm to che vuoi position over {self.RAISE_DURATION}s")
        if not self._move_and_wait(raise_pose, self.RAISE_DURATION):
            return "Failed to raise arm", SkillResult.FAILURE
        if self._cancelled:
            return "Italian arm wave cancelled", SkillResult.CANCELLED

        # Phase 2: Dramatic hold
        self.logger.info(f"Phase 2: Holding for {self.HOLD_DURATION}s")
        time.sleep(self.HOLD_DURATION)
        if self._cancelled:
            return "Italian arm wave cancelled", SkillResult.CANCELLED

        # Phase 3: Wrist bounces
        self.logger.info(f"Phase 3: {num_bounces} wrist bounces")
        bounce_up = self._apply_deltas(raise_pose, self.BOUNCE_UP_DELTAS, intensity)
        bounce_down = self._apply_deltas(raise_pose, self.BOUNCE_DOWN_DELTAS, intensity)

        for i in range(num_bounces):
            if self._cancelled:
                return "Italian arm wave cancelled", SkillResult.CANCELLED

            # Down flick
            if not self._move_and_wait(bounce_down, self.BOUNCE_HALF_CYCLE):
                return f"Failed at bounce {i + 1} (down)", SkillResult.FAILURE

            if self._cancelled:
                return "Italian arm wave cancelled", SkillResult.CANCELLED

            # Up return
            if not self._move_and_wait(bounce_up, self.BOUNCE_HALF_CYCLE):
                return f"Failed at bounce {i + 1} (up)", SkillResult.FAILURE

        # Phase 4: Smooth return
        self.logger.info(f"Phase 4: Returning to rest over {self.RETURN_DURATION}s")
        if not self._move_and_wait(rest, self.RETURN_DURATION):
            return "Failed to return to rest pose", SkillResult.FAILURE

        return "Italian arm wave completed", SkillResult.SUCCESS

    def _move_and_wait(self, joint_positions: list[float], duration: float) -> bool:
        """Send joint command and wait for the motion duration."""
        success = self.manipulation.move_to_joint_positions(
            joint_positions=joint_positions,
            duration=duration,
            blocking=False,
        )
        if not success:
            return False
        time.sleep(duration)
        return True

    def _scale_pose(self, pose: list[float], intensity: float) -> list[float]:
        """Scale a pose's joint values by intensity factor."""
        return [v * intensity for v in pose]

    def _apply_deltas(self, base: list[float], deltas: list[float], intensity: float) -> list[float]:
        """Apply scaled deltas to a base pose."""
        return [b + d * intensity for b, d in zip(base, deltas)]

    def cancel(self):
        self._cancelled = True
        return "Italian arm wave cancelled"
