# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import math

from rclpy.client import Client
from std_srvs.srv import Trigger

from innate import Mobility, Pose, Skill, SkillOutput, SkillReturn, resource
from mars_nav.localize_reply import LOW_CONFIDENCE, pose_in

LOCALIZE_SERVICE = "/localize"
SETTLE_S = 0.4
LOCALIZE_TIMEOUT_S = 30.0
AMCL_TIMEOUT_S = 5.0
AGREE_M = 0.5  # AMCL's first estimates from a seed scatter within its covariance (σ ≈ 0.3 m, 13°)
AGREE_RAD = math.radians(30)


class Relocalize(Skill):
    """Work out where the robot is on its map when it is lost: carried somewhere, switched
    on away from where it was, or navigation keeps failing because its position on the map
    is wrong. Matches one lidar scan against the whole map, without moving, and resets the
    navigation pose to the best match. Fails when another place fits the scan nearly as
    well: the pose is still reset to the best match, but it needs checking before driving.
    Needs a map loaded in navigation mode."""

    mobility: Mobility
    pose: Pose | None

    @resource
    def localizer(self) -> Client:
        """grid_localizer's /localize, the service behind the app's Locate button. The node's client is
        reused across runs: destroying one under the spinning executor races it (InvalidHandle)."""
        if self.node is None:
            self.fail("No ROS node to reach the localizer from.")
        existing = next((client for client in self.node.clients if client.srv_name == LOCALIZE_SERVICE), None)
        return existing or self.node.create_client(Trigger, LOCALIZE_SERVICE)

    def execute(self) -> SkillReturn:
        if self.wait_for(lambda: self.localizer.service_is_ready() or None, timeout=2.0) is None:
            self.fail("The localizer isn't running; switch to navigation mode with a map loaded.")
        self.mobility.stop()
        self.sleep(SETTLE_S)
        call = self.localizer.call_async(Trigger.Request())
        if self.wait_for(lambda: call.done() or None, timeout=LOCALIZE_TIMEOUT_S) is None:
            self.fail("The localizer did not answer.")
        reply = call.result()
        if reply is None or not reply.success:
            self.fail(f"Could not localize: {reply.message if reply is not None else 'no reply'}.")
        found = pose_in(reply.message)
        if found is None:
            self.fail(f"Could not read where the localizer put me: {reply.message}.")
        if self.wait_for(lambda: _agrees(self.pose, found) or None, timeout=AMCL_TIMEOUT_S) is None:
            self.fail(f"{reply.message}, but AMCL has not taken the new pose.")
        if reply.message.startswith(LOW_CONFIDENCE):
            self.fail(
                f"Not sure where I am: {reply.message}. Another place fits the scan nearly as well; I've been placed at "
                "the best match, but check my position on the map or place me from the app before driving."
            )
        return SkillOutput(f"{reply.message}.")


def _agrees(pose: Pose | None, found: tuple[float, float, float]) -> bool:
    """Whether AMCL's estimate is the found pose, within the scatter of a fresh seed."""
    if pose is None:
        return False
    x, y, theta = found
    turn = math.atan2(math.sin(pose.theta - theta), math.cos(pose.theta - theta))
    return math.hypot(pose.x - x, pose.y - y) <= AGREE_M and abs(turn) <= AGREE_RAD
