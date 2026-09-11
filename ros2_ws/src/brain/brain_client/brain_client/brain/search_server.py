# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The ``/brain/search_memory`` action: spatial-memory recall as a capability.

The capability server pattern (arm_sdk_server, the nav stack): what a search
needs — the remembered frames, the transport, the credentials — stays in the
brain process with :class:`MemorySearch`; skills and SDK users reach it
through this action and never see any of it.

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
from brain_messages.action import SearchMemory
from rclpy.action import ActionServer, CancelResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor

from brain_client.brain.memory_search import verdict_text

if TYPE_CHECKING:
    from rclpy.action.server import ServerGoalHandle

    from brain_client.brain.memory_search import MemorySearch, SearchVerdict


class MemorySearchServer:
    def __init__(self, search: MemorySearch):
        self._search = search
        self._node = rclpy.create_node("memory_search_server")
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
        self._executor = MultiThreadedExecutor(num_threads=4)
        self._executor.add_node(self._node)
        self._thread = threading.Thread(target=self._executor.spin, name="memory-search-server", daemon=True)
        self._thread.start()

    def _execute(self, goal_handle: ServerGoalHandle) -> SearchMemory.Result:
        verdict = self._search.search(str(goal_handle.request.query))  # never raises: errors are verdicts
        goal_handle.succeed()
        return _to_result(verdict)

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
