# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Gaze-tracker lifecycle wrapper.

Starts/stops the (lazily imported) ``ROSPersonTracker`` based on brain-active state
and whether the current directive opts into gaze, and pauses/resumes it around
skill execution. The heavy tracker import is deferred so directives that don't use
gaze never pay for it.
"""

from __future__ import annotations

import threading
from collections.abc import Callable


def _tracker_class():
    from brain_client.perception.gaze import ROSPersonTracker

    return ROSPersonTracker


class GazeController:
    def __init__(self, node, state):
        self._node = node
        self._logger = node.get_logger()
        self._state = state
        self._tracker = None
        # Where the tracker sends its tilt target; None drives the head servo directly.
        self.head_sink: Callable[[float | None], None] | None = None
        self.face_sink: Callable[[], None] | None = None
        # pause() runs on the agent's loop thread; everything else on the ROS
        # executor. RLock because update() calls stop().
        self._lock = threading.RLock()

    def update(self) -> None:
        """Start or stop the tracker to match brain state + directive preference."""
        with self._lock:
            directive = self._state.current_directive
            if not self._state.is_brain_active or directive is None or not directive.uses_gaze():
                directive = None
            if directive is not None and self._tracker is None:
                try:
                    self._tracker = _tracker_class()(self._node, head_sink=self.head_sink, face_sink=self.face_sink)
                    self._tracker.start()
                    self._logger.info(f"👁️ Gaze tracker started for directive '{directive.id}'")
                except Exception as e:
                    self._logger.error(f"Failed to start gaze tracker: {e}")
                    self._tracker = None
            elif directive is None and self._tracker is not None:
                self.stop()

    def stop(self) -> None:
        with self._lock:
            if self._tracker is not None:
                try:
                    self._tracker.stop()
                    self._logger.info("👁️ Gaze tracker stopped")
                except Exception as e:
                    self._logger.error(f"Error stopping gaze tracker: {e}")
                self._tracker = None

    def pause(self) -> None:
        with self._lock:
            if self._tracker is not None and self._tracker.is_running:
                self._tracker.stop()
                self._logger.debug("👁️ Gaze paused for skill execution")

    def resume(self) -> None:
        with self._lock:
            if self._tracker is not None and not self._tracker.is_running:
                self._tracker.start()
                self._logger.debug("👁️ Gaze resumed after skill execution")
