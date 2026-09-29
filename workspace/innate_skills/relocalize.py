# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from rclpy.client import Client
from std_srvs.srv import Trigger

from innate import Mobility, Pose, Skill, SkillOutput, SkillReturn, resource
from mars_nav.grid_localizer import LOW_CONFIDENCE

SETTLE_S = 0.4
LOCALIZE_TIMEOUT_S = 30.0
AMCL_TIMEOUT_S = 5.0


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
        """grid_localizer's /localize, the service behind the app's Locate button."""
        if self.node is None:
            self.fail("No ROS node to reach the localizer from.")
        return self.node.create_client(Trigger, "/localize")

    def execute(self) -> SkillReturn:
        if self.wait_for(lambda: self.localizer.service_is_ready() or None, timeout=2.0) is None:
            self.fail("The localizer isn't running; switch to navigation mode with a map loaded.")
        self.mobility.stop()
        self.sleep(SETTLE_S)
        before = self.pose
        call = self.localizer.call_async(Trigger.Request())
        if self.wait_for(lambda: call.done() or None, timeout=LOCALIZE_TIMEOUT_S) is None:
            self.fail("The localizer did not answer.")
        reply = call.result()
        if reply is None or not reply.success:
            self.fail(f"Could not localize: {reply.message if reply is not None else 'no reply'}.")
        if self.wait_for(lambda: _newer(self.pose, before), timeout=AMCL_TIMEOUT_S) is None:
            self.fail(f"{reply.message}, but AMCL has not taken the new pose.")
        if reply.message.startswith(LOW_CONFIDENCE):
            self.fail(
                f"Not sure where I am: {reply.message}. Another place fits the scan nearly as well; I've been placed at "
                "the best match, but check my position on the map or place me from the app before driving."
            )
        return SkillOutput(f"{reply.message}.")


def _newer(now: Pose | None, before: Pose | None) -> bool | None:
    """True once AMCL has published an estimate after ``before``."""
    return True if now is not None and (before is None or now.stamp > before.stamp) else None
