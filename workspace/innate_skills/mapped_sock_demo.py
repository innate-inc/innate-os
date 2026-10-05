# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""One mapped rehearsal. Never reuse positions across runs or translate the base."""

import json
import math
import time
import uuid

from innate_skills.box_marker import MarkerPose, load_config
from innate_skills.drop_in_box_stationary import DropInBoxStationary
from innate_skills.pick_any_object import NAV_ARM, PickAnyObject
from innate_skills.pick_sock_stationary import PickSockStationary
from innate_skills.sock_layout import angle_error, body_xy, parse_scene, world_xy
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String

from innate import Head, Llm, MainImage, Mobility, Odometry, Skill, SkillReturn, vision
from innate.exceptions import SkillFailed
from innate.geometry import IMG_H, IMG_W, pixel_to_floor

TOPIC = "/brain/sock_layout"


class RotationOnly:
    """Capability boundary shared by every composed child; navigation is absent."""

    def __init__(self, mobility):
        self._mobility = mobility

    def stop(self):
        self._mobility.stop()

    def send_cmd_vel(self, linear_x=0.0, angular_z=0.0, duration=None):
        if not math.isfinite(linear_x) or abs(linear_x) > 1e-9:
            self.stop()
            raise SkillFailed("Stationary demo forbids base translation")
        if not math.isfinite(angular_z) or abs(angular_z) > 0.8:
            self.stop()
            raise SkillFailed("Invalid stationary rotation speed")
        self._mobility.send_cmd_vel(linear_x=0.0, angular_z=angular_z, duration=duration)


class PickMappedSock(PickSockStationary):
    """Arm-only grasp at supplied floor coordinates in metres; no model or wheel motion."""

    def _close_twist_lift(self, x, y, roll, pitch, yaw):
        # One attempt, with the inherited gripper check after lifting.
        return PickAnyObject._close_twist_lift(self, x, y, roll, pitch, yaw)

    def execute(self, x: float, y: float, description: str) -> SkillReturn:
        if not all(math.isfinite(v) for v in (x, y)):
            raise SkillFailed("Invalid mapped sock coordinates")
        self._target_description = description
        self._grip_strength = None
        self._holding = self._carried = False
        self._last_seen = None
        self._coasts = 0
        self.mobility.stop()
        try:
            self.check_cancelled()
            self.head.set_position(int(round(self._p["tilt_deg"])))
            self.overlay.begin(description, stages=["grasp", "verify"], frame=(IMG_W, IMG_H))
            self._grasp_at(description, (x, y))
            if not self._grasp_verified(description, None):
                self._holding = False
                raise SkillFailed("Empty mapped grasp; reposition and remap")
            return "Mapped sock held; gripper verified."
        finally:
            self.mobility.stop()
            self._rest_arm(keep_grip=self._holding)
            self.head.set_position(0)


class DropMappedSock(DropInBoxStationary):
    """Rotation-only marker alignment, right-offset release and wrist shake."""

    def _release_xy(self, near_x, near_y):
        x, _ = self.manipulation.clamp_reach(near_x + self._p["drop_inset"], near_y)
        return x, near_y - 0.15


class MappedSockDemo(Skill):
    """Map exactly three visible floor socks, find the tagged box, then collect all three.
    Only rotates in place and moves the arm. The operator sets reachable distances.
    Every invocation maps a fresh layout. Failure or stop ends the run; never auto-retry.
    """

    head: Head
    mobility: Mobility
    odom: Odometry
    main_image: MainImage
    pickup: PickMappedSock
    drop: DropMappedSock
    llm: Llm = Llm("google:gemini-3.6-flash", thinking="minimal", extra_body="{}")

    def _pose(self):
        p = self.odom
        if not all(math.isfinite(v) for v in (p.x, p.y, p.theta, p.stamp)):
            raise SkillFailed("Invalid odometry; remap")
        if abs(time.time() - p.stamp) > 1.0:
            raise SkillFailed("Odometry is stale; stopped")
        if hasattr(self, "_origin") and math.hypot(p.x - self._origin.x, p.y - self._origin.y) > 0.03:
            raise SkillFailed("Robot shifted more than 3 cm; reposition and remap")
        return p

    def _publish(self):
        self._pub.publish(String(data=json.dumps(self._layout)))

    def _turn_to(self, heading):
        """Bounded closed-loop rotation. Linear command is ALWAYS zero."""
        speed = 0.0
        deadline = time.monotonic() + 15
        last = time.monotonic()
        stable = 0
        try:
            while time.monotonic() < deadline:
                self.check_cancelled()
                p = self._pose()
                error = angle_error(heading, p.theta)
                if abs(error) < math.radians(2) and abs(p.angular_velocity) < 0.08:
                    stable += 1
                    if stable >= 3:
                        return
                else:
                    stable = 0
                now = time.monotonic()
                dt = min(now - last, 0.1)
                last = now
                target = max(-0.6, min(0.6, error * 2.0))
                # Gentle acceleration, firm braking and immediate direction recovery.
                rate = 1.6 if speed * target < 0 or abs(target) < abs(speed) else 0.8
                speed += max(-rate * dt, min(rate * dt, target - speed))
                self.mobility.send_cmd_vel(linear_x=0.0, angular_z=speed, duration=0.15)
                self.sleep(0.04)
            raise SkillFailed("Rotation did not settle; stopped")
        finally:
            self.mobility.stop()

    def _marker(self, detector, timeout=1.0):
        deadline = time.monotonic() + timeout
        previous = self.main_image
        samples = []
        while time.monotonic() < deadline:
            self.check_cancelled()
            self._pose()
            frame = self.main_image
            if frame and frame is not previous:
                previous = frame
                pose = detector.detect(vision.b64_to_gray(frame))
                if pose is None:
                    samples.clear()
                else:
                    xy = tuple(float(v) for v in pose[:2, 3])
                    if samples and math.dist(samples[-1], xy) > 0.015:
                        samples.clear()
                    samples.append(xy)
                    if len(samples) >= 3:
                        return xy
            self.sleep(0.04)
        return None

    def _check_grasp_reach(self, radius):
        p = self.pickup._p
        x = radius - p["grasp_x_off"]
        cx, cy = self.pickup.manipulation.clamp_reach(x, 0)
        if abs(cx - x) > 0.002 or abs(cy) > 0.002:
            raise SkillFailed("A sock is outside stationary arm reach; reposition and remap")
        if not any(
            self.pickup.manipulation.reachable(x, 0, z, pitch=p["arm_pitch"]) for z in (p["hover_z"], 0.12, 0.10)
        ):
            raise SkillFailed("A sock has no reachable overhead approach")
        if not self.pickup.manipulation.reachable(x, 0, 0.08, pitch=p["arm_pitch"]):
            raise SkillFailed("A sock has no reachable grasp approach")

    def execute(self) -> SkillReturn:
        self.mobility.stop()
        if hasattr(self, "_origin"):
            del self._origin  # a new explicit run may start after operator repositioning
        self._origin = self._pose()
        guard = RotationOnly(self.mobility)
        self.pickup.mobility = guard
        self.drop.mobility = guard
        self._layout = {
            "version": 1,
            "run": uuid.uuid4().hex,
            "frame": "odom",
            "origin": {"x": self._origin.x, "y": self._origin.y, "theta": self._origin.theta},
            "status": "mapping",
            "points": [],
        }
        self._pub = self.node.create_publisher(
            String,
            TOPIC,
            QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL, reliability=ReliabilityPolicy.RELIABLE),
        )
        timer = self.node.create_timer(0.5, self._publish)
        try:
            self._publish()
            config = load_config()
            tilt = self.pickup._p["tilt_deg"]
            self.head.set_position(int(round(tilt)))
            self.pickup.manipulation.move_joints(NAV_ARM, duration=0.8)
            old = self.main_image
            self.sleep(0.3)
            image = self.wait_for(lambda: self.main_image if self.main_image != old else None, timeout=3)
            capture = self._pose()
            answer = self.llm.ask(
                image,
                """Identify exactly three separate socks lying on the floor, outside every box.
Return JSON only: {"socks":[{"description":"color and distinguishing features","box_2d":[ymin,xmin,ymax,xmax]}],
"boxes":[[ymin,xmin,ymax,xmax]]}. Coordinates normalized 0..1000.
Include all visible boxes. Do not invent socks or include socks inside boxes.""",
                logger=self.logger,
                retries=1,
            )
            if abs(angle_error(self._pose().theta, capture.theta)) > math.radians(2):
                raise SkillFailed("Robot rotated during mapping; remap")
            try:
                targets = parse_scene(answer, pixel_to_floor, tilt)
            except (ValueError, TypeError, KeyError) as e:
                raise SkillFailed(f"Cannot map rehearsal: {e}") from e
            for i, target in enumerate(targets, 1):
                self._check_grasp_reach(math.hypot(*target["xy"]))
                x, y = world_xy(*target["xy"], capture)
                self._layout["points"].append(
                    {
                        "id": f"sock-{i}",
                        "label": f"Sock {i}",
                        "description": target["description"],
                        "x": x,
                        "y": y,
                        "status": "pending",
                    }
                )
            # Locate box once, rotating only if outside the initial camera view.
            self.head.set_position(int(round(config["head_tilt_deg"])))
            self.sleep(0.3)
            detector = MarkerPose(config["marker_id"], config["marker_size_m"], config["head_tilt_deg"])
            found = None
            for step in range(5):
                if step:
                    self._turn_to(capture.theta - step * math.pi / 2)
                found = self._marker(detector)
                if found is not None:
                    break
            if found is None:
                raise SkillFailed("Box marker not found after one full turn")
            bx, by = world_xy(*found, self._pose())
            box = {"id": "box", "label": "Box", "x": bx, "y": by, "status": "target"}
            self._layout["points"].append(box)
            # The box distance is operator-controlled, but actual arm IK still matters.
            nx, ny = config["near_xy"]
            dx, dy = self.drop._release_xy(nx, ny)
            for x, z in ((self.drop._p["carry_x"], 0.28), (dx, self.drop.RELEASE_Z), (dx, self.drop.CLEARANCE_Z)):
                if not self.drop.manipulation.reachable(x, dy, z, pitch=self.drop._p["arm_pitch"]):
                    raise SkillFailed("Right-offset box release is unreachable")
            self._layout["status"] = "ready"
            self._publish()
            self.sleep(0.5)
            for sock in self._layout["points"][:3]:
                self._layout["status"] = f"picking {sock['label']}"
                self._publish()
                p = self._pose()
                self._turn_to(math.atan2(sock["y"] - p.y, sock["x"] - p.x))
                x, y = body_xy(sock["x"], sock["y"], self._pose())
                self.pickup(x=x, y=y, description=sock["description"])
                sock["status"] = "held"
                self._layout["status"] = f"dropping {sock['label']}"
                self._publish()
                p = self._pose()
                self._turn_to(math.atan2(by - p.y, bx - p.x))
                self.head.set_position(int(round(config["head_tilt_deg"])))
                self.sleep(0.2)
                check = self._marker(detector)
                if check is None or math.dist(world_xy(*check, self._pose()), (bx, by)) > 0.05:
                    raise SkillFailed("Box moved or heading drifted; hold sock and remap")
                self.drop()
                self._pose()
                sock["status"] = "released"
                self._publish()
            self._layout["status"] = "complete"
            return "Three mapped socks grasped and released into the taught box pose. Landings not visually verified."
        except BaseException:
            self._layout["status"] = "stopped — remap before retry"
            raise
        finally:
            self.mobility.stop()
            timer.cancel()  # run-node teardown destroys it after the executor releases it
            self._publish()
            self.storage["last_layout"] = self._layout
            self.head.set_position(0)
