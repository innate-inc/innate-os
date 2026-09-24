# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Race-starter arm cue synchronized with the spoken GO command."""

from innate import Manipulation, Skill, SkillReturn

# Five joints deliberately preserve the current gripper command.
# The ready pose is a tall, straight arm; the signal pose is a compact
# forward/down sweep that reads as a start cue rather than a wave.
READY_POSE = [0.0, -0.2, -0.6, -0.7, 0.0]
SIGNAL_POSE = [0.0, 0.2, -0.2, -0.6, 0.0]
REST_POSE = [1.5708, -1.2195, 1.5723, -0.3, 0.0]

STREAM_PERIOD = 0.08


class RaceStartGesture(Skill):
    """Give a race-start cue: raise the arm, say GO, sweep it down, then rest.

    This skill owns the spoken word "GO" so the voice and gesture stay
    synchronized. Do not say "GO" separately before calling it.
    """

    manipulation: Manipulation

    def _stream_pose(self, pose: list[float], duration: float, max_speed: float) -> None:
        elapsed = 0.0
        while elapsed < duration:
            self.manipulation.stream_joints(pose, max_speed=max_speed)
            step = min(STREAM_PERIOD, duration - elapsed)
            self.sleep(step)
            elapsed += step

    def execute(self) -> SkillReturn:
        self.feedback("Raising the starter arm")
        try:
            self._stream_pose(READY_POSE, duration=1.8, max_speed=1.2)
            self.sleep(0.35)

            # Give TTS a fraction of a second to begin playback, then make
            # the decisive downstroke while "GO" is being heard.
            self.say("GO!")
            self.sleep(0.12)
            self.feedback("GO")
            self._stream_pose(SIGNAL_POSE, duration=0.55, max_speed=1.8)
            self.sleep(0.3)

            self.feedback("Returning the arm to rest")
            self._stream_pose(REST_POSE, duration=1.8, max_speed=1.2)
            return "Said GO and completed the race-start gesture"
        finally:
            # Normal completion and cancellation both stop the live stream.
            # On cancellation the arm holds its current pose; it does not
            # surprise the user with a post-Stop return-to-rest movement.
            self.manipulation.stream_stop()
