# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Deterministic actuator composition for idle, clips, speech, gaze and arbitration."""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, TypedDict

from brain_client.expressive.basis import Act, ActuatorPose, Basis
from brain_client.expressive.breathing import Breathing
from brain_client.expressive.speech import SpeechSway

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

    from brain_client.expressive.basis import Projector, Vector
    from brain_client.expressive.motion import Clip

logger = logging.getLogger(__name__)
PoseCallback = Callable[[ActuatorPose], None]
_PARTS = {
    "arm": slice(Act.J1, Act.HEAD_DEG),
    "head": slice(Act.HEAD_DEG, Act.BASE_YAW),
    "base": slice(Act.BASE_YAW, None),
}


class AnimatorState(TypedDict):
    playing: bool
    name: str
    t: float
    duration: float
    idle: bool
    masked: list[str]
    speaking: bool


def _smoothstep(t: float, start: float, duration: float) -> float:
    weight = min(1.0, max(0.0, (t - start) / duration)) if duration > 0.0 else 1.0
    return weight * weight * (3.0 - 2.0 * weight)


class _Ramp:
    def __init__(self, value: float) -> None:
        self.source = self.target = value
        self.start = self.duration = 0.0

    def at(self, t: float) -> float:
        return self.source + (self.target - self.source) * _smoothstep(t, self.start, self.duration)

    def to(self, value: float, t: float, duration: float) -> None:
        if value == self.target:
            return
        self.source = self.at(t)
        self.target = value
        self.start, self.duration = t, duration


class _PlanMotion(Protocol):
    @property
    def name(self) -> str: ...

    def sample(self, t: float) -> Vector: ...


class _Source(Protocol):
    def sample(self, t: float) -> Vector: ...

    def moving(self, t: float) -> float: ...

    def settle(self, t: float) -> _Source: ...


@dataclass(frozen=True)
class _Idle:
    motion: _PlanMotion
    basis: Basis
    project: Projector | None

    def sample(self, t: float) -> Vector:
        return self.basis.synthesize(self.motion.sample(t), self.project).vector

    def moving(self, t: float) -> float:
        return 0.0

    def settle(self, t: float) -> _Source:
        return self


@dataclass(frozen=True)
class _Still:
    vector: Vector

    def sample(self, t: float) -> Vector:
        return self.vector.copy()

    def moving(self, t: float) -> float:
        return 0.0

    def settle(self, t: float) -> _Source:
        return self


@dataclass(frozen=True)
class _Playing:
    clip: Clip
    start: float

    @property
    def end(self) -> float:
        return self.start + self.clip.duration

    def sample(self, t: float) -> Vector:
        return self.clip.sample(t - self.start)

    def moving(self, t: float) -> float:
        return 1.0

    def settle(self, t: float) -> _Source:
        return self


class _Crossfade:
    def __init__(self, source: _Source, target: _Source, start: float, duration: float) -> None:
        self.source, self.target = source, target
        self.start, self.duration = start, duration

    def sample(self, t: float) -> Vector:
        weight = _smoothstep(t, self.start, self.duration)
        return (1.0 - weight) * self.source.sample(t) + weight * self.target.sample(t)

    def moving(self, t: float) -> float:
        weight = _smoothstep(t, self.start, self.duration)
        return (1.0 - weight) * self.source.moving(t) + weight * self.target.moving(t)

    def settle(self, t: float) -> _Source:
        if t >= self.start + self.duration:
            return self.target.settle(t)
        self.source = self.source.settle(t)
        return self


class Animator:
    """Hooks take effect on the next tick; inject the hook clock for offline playback."""

    def __init__(
        self,
        fps: float = 30.0,
        idle: _PlanMotion | None = None,
        blend_s: float = 0.4,
        basis: Basis | None = None,
        project: Projector | None = None,
        speech_latency_s: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        speech_sway: float = 1.0,
        speech_sway_in_motion: float = 0.5,
    ) -> None:
        if not math.isfinite(fps) or fps <= 0.0:
            raise ValueError("fps must be finite and positive")
        if not math.isfinite(blend_s) or blend_s < 0.0:
            raise ValueError("blend_s must be finite and nonnegative")
        self.fps, self.blend_s = fps, blend_s
        self._basis = basis if basis is not None else Basis.load()
        self._project = project
        self._idle = _Idle(idle if idle is not None else Breathing(), self._basis, project)
        self._source: _Source = self._idle
        self._current: _Playing | None = None
        self._queue: deque[Clip] = deque()
        self._interrupt = False
        self._enter_from: tuple[Vector, float] | None = None
        self._last: tuple[float, Vector] | None = None
        self._generation = 0
        self._sway = _Ramp(speech_sway)
        self._sway_in_motion = _Ramp(speech_sway_in_motion)
        self._speech = SpeechSway(latency_s=speech_latency_s)
        self._gaze_weight = _Ramp(0.0)
        self._gaze_value = _Ramp(float(self._basis.neutral[Act.HEAD_DEG]))
        self._masks = {part: _Ramp(1.0) for part in _PARTS}
        self._clock = clock
        self._lock = threading.Lock()
        self._closing = threading.Event()
        self._thread: threading.Thread | None = None
        self._pose_callbacks: list[PoseCallback] = []

    def play(self, clip: Clip | Callable[[], Clip], *, queue: bool = False) -> None:
        with self._lock:
            if not queue:
                self._generation += 1
            generation = self._generation
        if callable(clip):
            threading.Thread(target=self._make, args=(clip, queue, generation), daemon=True).start()
            return
        self._schedule(clip, queue, generation)

    def enter_from(self, pose: ActuatorPose, seconds: float = 1.5) -> None:
        """Crossfade from ``pose`` (where the robot is now) into the animation over ``seconds`` on the next tick."""
        with self._lock:
            self._enter_from = (pose.vector.copy(), seconds)
            self._last = None

    def stop(self) -> None:
        with self._lock:
            self._generation += 1
            self._queue.clear()
            self._interrupt = True

    @property
    def playing(self) -> Clip | None:
        with self._lock:
            return self._current.clip if self._current is not None else None

    @property
    def masked(self) -> dict[str, bool]:
        """Requested exclusions, including parts still easing toward neutral."""
        with self._lock:
            return {part: ramp.target == 0.0 for part, ramp in self._masks.items()}

    def state(self, t: float) -> AnimatorState:
        with self._lock:
            current = self._current
            return {
                "playing": current is not None,
                "name": current.clip.name if current is not None else self._idle.motion.name,
                "t": min(current.clip.duration, max(0.0, t - current.start)) if current is not None else 0.0,
                "duration": current.clip.duration if current is not None else 0.0,
                "idle": current is None,
                "masked": [part for part, ramp in self._masks.items() if ramp.target == 0.0],
                "speaking": self._speech.speaking(t),
            }

    def feed_speech(self, pcm: NDArray[np.generic], sample_rate: int) -> None:
        with self._lock:
            self._speech.feed(pcm, sample_rate, at=self._clock())

    def interrupt_speech(self) -> None:
        with self._lock:
            self._speech.interrupt(self._clock())

    def set_speech_sway(self, sway: float | None = None, *, in_motion: float | None = None) -> None:
        with self._lock:
            now = self._clock()
            if sway is not None:
                self._sway.to(sway, now, self.blend_s)
            if in_motion is not None:
                self._sway_in_motion.to(in_motion, now, self.blend_s)

    def set_mask(self, *, arm: bool = True, head: bool = True, base: bool = True) -> None:
        with self._lock:
            now = self._clock()
            for part, enabled in (("arm", arm), ("head", head), ("base", base)):
                self._masks[part].to(float(enabled), now, self.blend_s)

    def set_gaze(self, head_deg: float | None) -> None:
        with self._lock:
            now = self._clock()
            self._gaze_weight.to(float(head_deg is not None), now, self.blend_s)
            if head_deg is not None:
                self._gaze_value.to(head_deg, now, self.blend_s)

    def tick(self, t: float) -> ActuatorPose:
        """Sample on an increasing time sequence without reading the wall clock or invoking callbacks."""
        with self._lock:
            finished = self._current is not None and t >= self._current.end
            if self._interrupt or finished or (self._current is None and self._queue):
                self._interrupt = False
                self._enter(self._queue.popleft() if self._queue else None, t)
            if self._enter_from is not None:
                vector, seconds = self._enter_from
                self._enter_from = None
                self._source = _Crossfade(_Still(vector), self._source.settle(t), t, seconds)
            self._source = self._source.settle(t)
            moving = self._source.moving(t)
            gain = self._sway.at(t) * (1.0 - moving + moving * self._sway_in_motion.at(t))
            vector = self._source.sample(t) + gain * self._basis.offset(self._speech.sample(t))
            neutral = self._basis.neutral
            vector[Act.HEAD_DEG] += self._gaze_weight.at(t) * (self._gaze_value.at(t) - neutral[Act.HEAD_DEG])
            for part, indices in _PARTS.items():
                vector[indices] = neutral[indices] + self._masks[part].at(t) * (vector[indices] - neutral[indices])
            vector = self._basis.clamp(vector)
            if self._last is not None and t > self._last[0]:
                vector = self._basis.limit(self._last[1], vector, t - self._last[0])
            self._last = (t, vector)
            return ActuatorPose(vector.copy())

    def on_pose(self, callback: PoseCallback) -> PoseCallback:
        """Register a tick-thread callback; each callback receives its own pose vector."""
        with self._lock:
            self._pose_callbacks.append(callback)
        return callback

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                logger.warning("Animator already running; start() ignored")
                return
            self._closing.clear()
            self._thread = threading.Thread(target=self._run, name="mars-expressive", daemon=True)
            self._thread.start()

    def close(self) -> None:
        with self._lock:
            self._closing.set()
            thread = self._thread
        if thread is None or thread is threading.current_thread():
            return
        thread.join()
        with self._lock:
            if self._thread is thread:
                self._thread = None

    def _make(self, make_clip: Callable[[], Clip], queue: bool, generation: int) -> None:
        try:
            self._schedule(make_clip(), queue, generation)
        except Exception as error:  # noqa: BLE001 — a failed generator must not stop playback
            logger.warning("Could not make a clip to play: %s", error)

    def _schedule(self, clip: Clip, queue: bool, generation: int) -> None:
        prepared = clip.to_actuators(self._basis, self._project).resample(self.fps)
        with self._lock:
            if generation != self._generation:
                logger.info("Dropped %s: superseded while it was being made", clip.name)
                return
            if not queue:
                self._queue.clear()
                self._interrupt = True
            self._queue.append(prepared)

    def _enter(self, clip: Clip | None, t: float) -> None:
        self._current = _Playing(clip, t) if clip is not None else None
        target: _Source = self._current if self._current is not None else self._idle
        self._source = _Crossfade(self._source.settle(t), target, t, self.blend_s)

    def _run(self) -> None:
        period = 1.0 / self.fps
        next_tick = self._clock()
        last_error = last_overrun = -math.inf
        while not self._closing.is_set():
            pose = self.tick(next_tick)
            with self._lock:
                callbacks = tuple(self._pose_callbacks)
            for callback in callbacks:
                try:
                    callback(ActuatorPose(pose.vector.copy()))
                except Exception as error:  # noqa: BLE001 — a failing robot link must not kill the loop
                    if next_tick - last_error >= 1.0:
                        logger.error("on_pose callback failed: %s", error)
                        last_error = next_tick
            next_tick += period
            now = self._clock()
            if now > next_tick:
                skipped = math.ceil((now - next_tick) / period)
                next_tick += skipped * period
                if now - last_overrun >= 1.0:
                    logger.warning("Animator behind schedule, skipped %d ticks", skipped)
                    last_overrun = now
            self._closing.wait(max(0.0, next_tick - self._clock()))
