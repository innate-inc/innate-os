# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The spatial-memory actions: ``/brain/search_memory`` (which remembered view
serves a need) and ``/brain/recognize_place`` (which remembered views show
where the robot is now), as capabilities.

The capability server pattern (arm_sdk_server, the nav stack): the state that
makes them fast and safe — the Gemini context cache, the transport, the
credentials — stays in the brain process with :class:`MemorySearch` and
:class:`PlaceRecognition`; skills and SDK users reach it through these actions
and never see any of it.

Runs on its own node and spin thread because a search blocks for seconds and
the brain node is spun single-threaded — recall must never stall a turn.
Cancellation is rejected by design: the network call can't be unwound, so a
caller that stops waiting simply abandons the goal and the verdict is dropped
(the skill layer's Stop already works exactly that way).
"""

from __future__ import annotations

import base64
import threading
from typing import TYPE_CHECKING

import rclpy
from brain_messages.action import RecognizePlace, SearchMemory
from brain_messages.msg import PlaceMatch
from rclpy.action import ActionServer, CancelResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor

from brain_client.brain.memory_search import verdict_text
from brain_client.brain.place_recognition import PlaceVerdict, View

if TYPE_CHECKING:
    from rclpy.action.server import ServerGoalHandle

    from brain_client.brain.memory_search import MemorySearch, SearchVerdict
    from brain_client.brain.place_recognition import PlaceRecognition


class MemorySearchServer:
    def __init__(self, search: MemorySearch, recognition: PlaceRecognition):
        self._search = search
        self._recognition = recognition
        self._node = rclpy.create_node("memory_search_server", start_parameter_services=False)
        # Reentrant + multithreaded (the arm_sdk_server pattern): an execute
        # callback blocks its thread for the whole search, and goal/result
        # requests from other callers must still be serviced meanwhile.
        self._server = ActionServer(
            self._node,
            SearchMemory,
            "/brain/search_memory",
            execute_callback=self._execute,
            cancel_callback=lambda _: CancelResponse.REJECT,
            callback_group=ReentrantCallbackGroup(),
        )
        self._recognize_server = ActionServer(
            self._node,
            RecognizePlace,
            "/brain/recognize_place",
            execute_callback=self._execute_recognize,
            cancel_callback=lambda _: CancelResponse.REJECT,
            callback_group=ReentrantCallbackGroup(),
        )
        self._executor = MultiThreadedExecutor(num_threads=4)
        self._executor.add_node(self._node)
        self._thread = threading.Thread(target=self._executor.spin, name="memory-search-server", daemon=True)
        self._thread.start()

    def _execute(self, goal_handle: ServerGoalHandle) -> SearchMemory.Result:
        verdict = self._search.search(str(goal_handle.request.query))  # never raises: errors are verdicts
        goal_handle.succeed()
        return _to_result(verdict)

    def _execute_recognize(self, goal_handle: ServerGoalHandle) -> RecognizePlace.Result:
        goal = goal_handle.request
        try:
            views = [
                View(base64.b64decode(image), str(label))
                for image, label in zip(goal.images_b64, goal.labels, strict=True)
            ]
        except ValueError as error:
            goal_handle.succeed()
            return _to_recognize_result(PlaceVerdict(error=f"malformed goal: {error}"))
        verdict = self._recognition.recognize(views, str(goal.context))  # never raises: errors are verdicts
        goal_handle.succeed()
        return _to_recognize_result(verdict)

    def shutdown(self) -> None:
        self._executor.shutdown(timeout_sec=2.0)
        self._thread.join(timeout=2.0)
        if self._thread.is_alive():
            # A search is still concluding on the spin thread; destroying the
            # node under it is the InvalidHandle → SIGABRT race. The daemon
            # thread dies with the process — leak the node instead.
            return
        self._node.destroy_node()


def _to_result(verdict: SearchVerdict) -> SearchMemory.Result:
    result = SearchMemory.Result()
    result.found = verdict.found
    result.message = verdict_text(verdict)
    result.explanation = verdict.explanation
    result.error = verdict.error
    result.latency_sec = float(verdict.latency_sec)
    result.cached = verdict.cached
    if verdict.memory is not None:
        result.x = verdict.memory.x
        result.y = verdict.memory.y
        result.theta = verdict.memory.theta
        result.seen_stamp = verdict.memory.stamp
    if verdict.image:
        result.image_b64 = base64.b64encode(verdict.image).decode()
    return result


def _to_recognize_result(verdict: PlaceVerdict) -> RecognizePlace.Result:
    result = RecognizePlace.Result()
    result.error = verdict.error
    result.turn_deg = float(verdict.turn_deg)
    result.forward_m = float(verdict.forward_m)
    result.move_reason = verdict.move_reason
    result.latency_sec = float(verdict.latency_sec)
    result.matches = [
        PlaceMatch(
            frame=match.memory.id,
            view=match.view,
            confidence=float(match.confidence),
            x=match.memory.x,
            y=match.memory.y,
            theta=match.memory.theta,
            evidence=match.evidence,
        )
        for match in verdict.matches
    ]
    return result
