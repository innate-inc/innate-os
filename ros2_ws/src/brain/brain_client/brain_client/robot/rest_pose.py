# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Fold the arm to its rest pose when an agent starts.

Same mechanism Mad speed mode uses to brace the arm (``fold_arm_for_mad_mode``
in ``mars_control/app.cpp``): one ``/mars/arm/goto_js_v2`` call, not the
arm_rest_position skill — activation must not occupy the single skill slot the
agent's own first turn needs. Fire-and-forget, so the agent starts thinking
while the arm folds.

One command, deliberately: Manipulation.rest() re-commands the pose to settle
the servos under a shifted load, but it runs inside a skill that owns the arm.
Nothing here owns it, and a second command decided on the ROS callback thread
cannot be sequenced against an agent claiming the arm on its own thread.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mars_msgs.srv import GotoJS
from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy
from sensor_msgs.msg import JointState
from std_msgs.msg import Float64MultiArray

from brain_client.robot.manipulation import Manipulation

if TYPE_CHECKING:
    from rclpy.node import Node
    from rclpy.task import Future

    from brain_client.core.state import BrainState

GOTO_JS_SERVICE = "/mars/arm/goto_js_v2"
COMMAND_STATE_TOPIC = "/mars/arm/command_state"

_FOLD_DURATION_S = 3.0

_LATCHED_QOS = QoSProfile(
    depth=1,
    durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
    reliability=QoSReliabilityPolicy.RELIABLE,
)


class ArmRestPose:
    """Sends the arm to :attr:`Manipulation.REST` on agent activation."""

    def __init__(self, node: Node, state: BrainState) -> None:
        self._logger = node.get_logger()
        self._state = state
        self._client = node.create_client(GotoJS, GOTO_JS_SERVICE)
        # Last COMMANDED j6 (the standing grip target). Transient local: a
        # brain restart while an object is held must not read it back as zero.
        self._grip: float | None = None
        node.create_subscription(JointState, COMMAND_STATE_TOPIC, self._on_command_state, _LATCHED_QOS)

    def fold(self) -> None:
        """Fold to rest, keeping the grip; returns once the goto is sent."""
        if self._state.primitive_running is not None:
            return  # a skill already owns the arm
        if not self._client.service_is_ready():
            self._logger.warn(f"[RestPose] {GOTO_JS_SERVICE} is not ready; the arm stays where it is")
            return
        request = GotoJS.Request()
        request.data = Float64MultiArray()
        # j6 carries the standing grip target rather than the rest pose's own:
        # it is current-based position control, so re-commanding it above that
        # target zeroes the preload and drops a held object.
        request.data.data = [*Manipulation.REST[:5], self._grip if self._grip is not None else Manipulation.REST[5]]
        request.time = _FOLD_DURATION_S
        self._logger.info("[RestPose] Folding the arm to its rest pose for the starting agent")
        self._client.call_async(request).add_done_callback(self._on_result)

    def _on_command_state(self, msg: JointState) -> None:
        if len(msg.position) >= 6:
            self._grip = float(msg.position[5])

    def _on_result(self, future: Future) -> None:
        response = future.result()
        if response is None or not response.success:
            self._logger.warn("[RestPose] The arm rejected the rest fold; leaving it where it is")
