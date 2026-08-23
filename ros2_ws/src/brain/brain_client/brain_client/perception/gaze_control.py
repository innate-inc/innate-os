# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Gaze lifecycle.

Starts/stops the lazily imported face tracker based on brain-active state
and whether the current directive opts into gaze, and pauses/resumes it around
skill execution.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

from rclpy.node import Node

from brain_client.perception.gaze_debug import GazeDebug, GazeStatus, gaze_debug
from brain_client.perception.person_follow import FollowStartResult

if TYPE_CHECKING:
    from brain_client.core.state import BrainState
    from brain_client.perception.gaze import ROSFaceTracker
    from brain_client.perception.pose_tracking import PoseTracker


def _tracker_class() -> type[ROSFaceTracker]:
    from brain_client.perception.gaze import ROSFaceTracker

    return ROSFaceTracker


class GazeLifecycle:
    def __init__(
        self,
        node: Node,
        state: BrainState,
        pose_tracker: PoseTracker,
        cmd_vel_topic: str,
        on_debug: Callable[[GazeDebug], None] | None = None,
    ) -> None:
        self._node = node
        self._logger = node.get_logger()
        self._state = state
        self._pose_tracker = pose_tracker
        self._cmd_vel_topic = cmd_vel_topic
        self._tracker: ROSFaceTracker | None = None
        self._on_debug = on_debug
        self.on_person_locked: Callable[[], None] | None = None
        self.on_follow_stopped: Callable[[str], None] | None = None
        # pause() runs on the agent's loop thread; everything else on the ROS
        # executor. RLock because update() calls stop().
        self._lock = threading.RLock()
        self._emit_debug(GazeStatus.OFF)

    def update(self) -> None:
        """Start or stop the tracker to match brain state + directive preference."""
        with self._lock:
            directive = self._state.current_directive
            if not self._state.is_brain_active or directive is None or not directive.uses_gaze():
                directive = None
            if directive is not None and self._tracker is None:
                tracker: ROSFaceTracker | None = None
                try:
                    tracker = _tracker_class()(
                        self._node,
                        cmd_vel_topic=self._cmd_vel_topic,
                        get_odom_pose=self._pose_tracker.odom_pose_xyt,
                        get_navigation_mode=lambda: self._pose_tracker.cur_nav_mode,
                        on_person_locked=self.on_person_locked,
                        on_follow_stopped=self.on_follow_stopped,
                        on_debug=self._on_debug,
                    )
                    tracker.start()
                    self._tracker = tracker
                    self._logger.info(f"👁️ Gaze tracker started for directive '{directive.id}'")
                except Exception as e:
                    self._logger.error(f"Failed to start gaze tracker: {e}")
                    if tracker is not None:
                        try:
                            tracker.close()
                        except Exception as close_error:
                            self._logger.error(f"Error cleaning up gaze tracker: {close_error}")
                    self._tracker = None
            elif directive is not None and self._tracker is not None and not self._tracker.is_running:
                self._tracker.start()
            elif directive is None and self._tracker is not None:
                self.stop()

    def stop(self) -> None:
        with self._lock:
            if self._tracker is not None:
                try:
                    self._tracker.close()
                    self._logger.info("👁️ Gaze tracker stopped")
                except Exception as e:
                    self._logger.error(f"Error stopping gaze tracker: {e}")
                self._tracker = None
            self._emit_debug(GazeStatus.OFF)

    def pause(self) -> None:
        with self._lock:
            if self._tracker is not None and self._tracker.is_running:
                self._tracker.pause()
                self._logger.debug("👁️ Gaze paused for skill execution")

    def resume(self) -> None:
        with self._lock:
            if self._tracker is not None and not self._tracker.is_running:
                self._tracker.start()
                self._logger.debug("👁️ Gaze resumed after skill execution")

    @property
    def is_following(self) -> bool:
        with self._lock:
            return self._tracker is not None and self._tracker.is_following

    def start_follow(self) -> FollowStartResult:
        with self._lock:
            if self._tracker is None:
                return FollowStartResult.NOT_RUNNING
            return self._tracker.start_follow()

    def stop_follow(self, reason: str = "") -> None:
        with self._lock:
            if self._tracker is not None:
                self._tracker.stop_follow(reason)

    def _emit_debug(self, status: GazeStatus) -> None:
        if self._on_debug is not None:
            self._on_debug(gaze_debug(status))
