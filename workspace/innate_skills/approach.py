# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Find a thing on the floor and drive the base until it sits in a chosen spot.

Extracted from pick_any_object so pick and drop share one tuned approach: the
constants here were measured on hardware and a fix has to land once, not twice.
Subclasses override `_detect_px` (what to look for, and which pixel of it is
the floor-contact point) and `_p` (where to park).
"""

import json
import math
import time
from typing import TYPE_CHECKING

from std_msgs.msg import String

from innate import (
    MainImage,
    Mobility,
    Odometry,
    Skill,
    resource,
    vision,
)
from innate import gemini as gemlib
from innate.exceptions import SkillFailed
from innate.geometry import IMG_H, IMG_W, floor_to_pixel, pixel_to_floor

if TYPE_CHECKING:
    from innate_proxy import ProxyClient

# Debug telemetry for webapp/debug/approach.html. A frame carries the JPEG the
# vision model actually saw (~55 kB of base64) so it only rides the Gemini
# looks; the flow servo emits image-less ticks the page overlays on the last
# frame, or rosbridge stalls under a 30 Hz image stream.
DEBUG_TOPIC = "/brain/approach_debug"
DEBUG_TICK_MIN_S = 0.1
# Only the servo loop is throttled. Throttling by "carries no image" instead
# dropped every one-off stage record that happened to land within 100 ms of
# the detection before it — which is all of them.
DEBUG_THROTTLED_STAGES = ("follow",)

APPROACH_PARAMS = {
    "tilt_deg": -20.0,
    "settle_s": 1.2,
    "sweet_x": 0.37,
    "box_y": 0.0,
    "box_half_px": 40.0,
    "box_half_v_px": 40.0,
    "accept_frac": 0.5,
    "box_steps": 6.0,
    "bearing_go_deg": 4.0,
    "follow_gain_ang": 0.3,
    "follow_gain_lin": 0.06,
    "rot_tol_deg": 2.5,
    "rot_kp": 1.2,
    "rot_wz_max": 0.5,
    "rot_wz_min": 0.15,
    "drive_tol_m": 0.015,
    "drive_kp": 0.3,
    "drive_v_max": 0.10,
    "drive_v_min": 0.04,
}

FOLLOW_TIMEOUT_S = 20.0


def inside_box(px, cu, cv, half_u, half_v=None):
    return abs(px[0] - cu) <= half_u and abs(px[1] - cv) <= (half_u if half_v is None else half_v)


class _FloorApproach(Skill):
    """Head-camera localize + base servo. Not a skill on its own — the leading
    underscore keeps it off the roster (workspace_import.live_registered_classes)."""

    mobility: Mobility
    # `| None` — best effort, every read is guarded: positioning falls back to
    # stepwise re-detection without head frames.
    main_image: MainImage | None
    odom: Odometry | None

    _p = APPROACH_PARAMS
    _debug_node = None
    _debug_pub = None
    _debug_last = 0.0

    @resource
    def _proxy(self) -> "ProxyClient | None":
        return gemlib.make_client()

    # --- debug telemetry ---

    def _debug(self, stage, *, image=None, **fields):
        """Publish one debug record. Servo ticks are rate-limited, every other
        stage always goes out. Never raises — telemetry must not be able to
        fail a run."""
        now = time.monotonic()
        if stage in DEBUG_THROTTLED_STAGES and now - self._debug_last < DEBUG_TICK_MIN_S:
            return
        self._debug_last = now
        if self.node is None:
            return
        # Rebuild per node, not once: the run's throwaway node is destroyed at
        # run end, so a publisher cached across runs would be bound to a dead
        # node and raise InvalidHandle.
        if self._debug_pub is None or self._debug_node is not self.node:
            self._debug_node = self.node
            self._debug_pub = self.node.create_publisher(String, DEBUG_TOPIC, 10)
        record = {"skill": self.name, "stage": stage, "t": now, "tilt_deg": self._p["tilt_deg"], **fields}
        if image is not None:
            record["image"] = str(image)
        try:
            self._debug_pub.publish(String(data=json.dumps(record)))
        except Exception as e:  # noqa: BLE001 — a debug page must never break the skill
            self.logger.warning(f"[{self.name}] debug publish failed: {e}")

    def _debug_sweet(self):
        """The sweet box as the page draws it, or None when it is off-image —
        _sweet_box raises there, and the caller is only reporting."""
        try:
            (cu, cv), half, accept = self._sweet_box()
        except SkillFailed:
            return None
        return {
            "center_px": [cu, cv],
            "outer_px": half[0],
            "outer_v_px": half[1],
            "accept_px": accept[0],
            "accept_v_px": accept[1],
            "xy": [self._p["sweet_x"], self._p["box_y"]],
        }

    # --- localize ---

    def _ask_head(self, question):
        """Settle the base, then put the current head frame to Gemini.
        -> (reply_text|None, frame|None)."""
        self.mobility.stop()
        self.sleep(self._p["settle_s"])
        img = self.main_image
        if not img:
            return None, None
        return gemlib.ask_image(self._proxy, img, question, logger=self.logger), img

    def _detect_px(self, prompt):
        """Head frame -> the target's floor-contact pixel, or None."""
        raise NotImplementedError

    def _localize_px(self, prompt):
        """Detect + back-project -> ((x,y)|None, pixel|None)."""
        px = self._detect_px(prompt)
        if px is None:
            return None, None
        xy = pixel_to_floor(px[0], px[1], self._p["tilt_deg"])
        if xy:
            self.logger.info(f"[{self.name}] px=({px[0]:.0f},{px[1]:.0f}) -> base_link ({xy[0]:.3f},{xy[1]:.3f})")
        self._debug("localize", point_px=list(px), target_xy=list(xy) if xy else None, sweet=self._debug_sweet())
        return xy, px

    def _localize_retry(self, prompt):
        """One retry: a single "not visible" is noise, not absence."""
        xy, px = self._localize_px(prompt)
        if px is None:
            xy, px = self._localize_px(prompt)
        return xy, px

    def _search(self, prompt):
        """Scan: straight, right 30°, left 60°. First hit wins. (+yaw=left)"""
        for i, turn in enumerate((0.0, -math.radians(30), math.radians(60))):
            if turn:
                if i == 1:
                    self.say("Scanning around for it.")
                # Best-effort: a rotate cut short (timeout / odom loss) still
                # changed the view, and the localize below measures from
                # wherever the base actually ended up.
                self._rotate_by(turn)
            xy, _px = self._localize_px(prompt)
            if xy is not None:
                return xy
        raise SkillFailed(f"Could not find '{prompt}' on the floor, even after scanning")

    # --- base motion ---

    def _odom_xyt(self):
        return self.mobility.odom_xyt(self.odom)

    def _rotate_by(self, angle):
        return self.mobility.rotate_by(
            self._odom_xyt,
            angle,
            kp=self._p["rot_kp"],
            wz_max=self._p["rot_wz_max"],
            wz_min=self._p["rot_wz_min"],
            tolerance=math.radians(self._p["rot_tol_deg"]),
            logger=self.logger,
        )

    def _drive(self, dist):
        return self.mobility.drive(
            self._odom_xyt,
            dist,
            kp=self._p["drive_kp"],
            v_max=self._p["drive_v_max"],
            v_min=self._p["drive_v_min"],
            tolerance=self._p["drive_tol_m"],
            logger=self.logger,
        )

    # --- position the base ---

    def _sweet_box(self):
        """(centre, (outer_u, outer_v), (accept_u, accept_v)). Stop only inside
        accept.

        The two axes carry different units of error and cannot share a
        tolerance: at the drop's 0.30 m park distance one image row is ~1.2 cm
        of RANGE but only ~0.8 mm of bearing, so the box widened enough to
        hold a clipped container's drifting centroid would also accept parking
        8 cm out — past the reach the release needs."""
        c = floor_to_pixel(self._p["sweet_x"], self._p["box_y"], self._p["tilt_deg"])
        if c is None or not (0 <= c[0] < IMG_W and 0 <= c[1] < IMG_H):
            raise SkillFailed("approach box off-image — check tilt_deg/sweet_x")
        hu, hv = self._p["box_half_px"], self._p["box_half_v_px"]
        frac = self._p["accept_frac"]
        return (c[0], c[1]), (hu, hv), (hu * frac, hv * frac)

    def _follow_into_box(self, seed_px):
        """Optical-flow base servo into the sweet box. No Gemini.
        Returns ('in_box'|'lost'|'timeout'|'noframe', px|None)."""
        raw = self.main_image
        prev = vision.b64_to_gray(raw) if raw else None
        if prev is None:
            return "noframe", None
        u, v = seed_px
        grid = vision.grid_pts(u, v)
        in_box = 0
        (cu, cv), _half, accept = self._sweet_box()
        sweet = self._debug_sweet()
        t0 = time.monotonic()
        while time.monotonic() - t0 < FOLLOW_TIMEOUT_S:
            # Only track NEW frames: the camera runs slower than this loop,
            # and a stale frame re-tracked would count one observation twice.
            # Compare by identity, not content: the provider builds one Image
            # per ROS message, and in sim consecutive frames of a static scene
            # are byte-identical, so `==` would deadlock waiting for a change.
            img = self.main_image
            if not img or img is raw:
                self.sleep(0.03)
                continue
            gray = vision.b64_to_gray(img)
            raw = img
            if gray is None:
                self.sleep(0.03)
                continue
            tracked = vision.track_point(prev, gray, grid)
            prev = gray
            if tracked is None:
                self.mobility.stop()
                self._debug("follow", note="lost", sweet=sweet)
                return "lost", None
            u, v = tracked
            grid = vision.grid_pts(u, v)
            if not (0 <= u < IMG_W and 0 <= v < IMG_H):
                self.mobility.stop()
                self._debug("follow", note="off-image", sweet=sweet)
                return "lost", None

            if inside_box((u, v), cu, cv, accept[0], accept[1]):
                in_box += 1
                self.mobility.stop()
                if in_box >= 3:
                    self._debug("follow", track_px=[u, v], note="in_box", sweet=sweet)
                    return "in_box", (u, v)
                self.sleep(0.03)
                continue
            in_box = 0

            # Deadband = accept (inner) box; right -> -wz, too close (low) -> -vx.
            wz = self.mobility.servo_vel(
                u - cu, self._p["follow_gain_ang"], self._p["rot_wz_min"], self._p["rot_wz_max"], accept[0]
            )
            vx = self.mobility.servo_vel(
                v - cv, self._p["follow_gain_lin"], self._p["drive_v_min"], self._p["drive_v_max"], accept[1]
            )
            self.mobility.send_cmd_vel(vx, wz, 0.15)
            self._debug("follow", track_px=[u, v], cmd=[vx, wz], sweet=sweet)
            self.sleep(0.03)
        self.mobility.stop()
        self._debug("follow", note="timeout", sweet=sweet)
        return "timeout", None

    def _position_failed(self, prompt):
        raise SkillFailed(f"Could not centre '{prompt}' in the approach box")

    def _position_above(self, prompt, xy):
        """Flow-follow into the sweet box; Gemini reseed/confirm. Stepwise if
        no cam. Raises SkillFailed if the target cannot be centred."""
        if not self.main_image:
            return self._position_stepwise(prompt, xy)

        seed = floor_to_pixel(xy[0], xy[1], self._p["tilt_deg"])
        _fallback_xy = xy
        lost = 0
        for _attempt in range(int(self._p["box_steps"])):
            if seed is None:
                xy, seed = self._localize_retry(prompt)
                if seed is None:
                    self._position_failed(prompt)
            # Arrived already? Checked BEFORE servoing, because a tracker that
            # loses its seed sends the loop back here with the base parked
            # where it should be — and without this it re-seeds until the
            # step budget runs out and reports a failure to centre something
            # that is already centred.
            (cu, cv), _half, accept = self._sweet_box()
            if xy is not None and inside_box(seed, cu, cv, accept[0], accept[1]):
                return xy
            result, _pt = self._follow_into_box(seed)
            if result == "noframe":
                return self._position_stepwise(prompt, xy)
            if result == "lost":
                # Optical flow has nothing to hold on a big, low-contrast face
                # filling the near field — a container's bottom edge at arm's
                # length is one beige line on beige floor. Two failures in a
                # row is the tracker telling us so; the odometry stepper closes
                # the last few centimetres on measurements instead of texture.
                lost += 1
                if lost >= 2:
                    return self._position_stepwise(prompt, xy if xy is not None else _fallback_xy)
                seed = None
                continue
            lost = 0
            xy2, px2 = self._localize_retry(prompt)
            if px2 is None:
                self._position_failed(prompt)
            if xy2 is not None and inside_box(px2, cu, cv, accept[0], accept[1]):
                return xy2
            xy, seed = xy2, (px2 if xy2 is not None else None)
        self._position_failed(prompt)

    def _position_stepwise(self, prompt, xy):
        """No-camera fallback: turn OR drive, re-detect, repeat.
        Raises SkillFailed if the target cannot be centred."""
        target_bearing = math.atan2(self._p["box_y"], self._p["sweet_x"])
        target_range = math.hypot(self._p["sweet_x"], self._p["box_y"])
        px = floor_to_pixel(xy[0], xy[1], self._p["tilt_deg"])
        for _step in range(int(self._p["box_steps"])):
            if px is None:
                xy, px = self._localize_retry(prompt)
                if px is None:
                    self._position_failed(prompt)
            (cu, cv), _half, accept = self._sweet_box()
            if xy is not None and inside_box(px, cu, cv, accept[0], accept[1]):
                return xy
            if xy is None:
                px = None
                continue
            bearing_err = math.atan2(xy[1], xy[0]) - target_bearing
            if abs(bearing_err) > math.radians(self._p["bearing_go_deg"]):
                moved = self._rotate_by(bearing_err)
            else:
                moved = self._drive(math.hypot(xy[0], xy[1]) - target_range)
            if not moved:
                # Odom loss or a stuck base: this closed-odometry stepper
                # cannot make progress, so burning the remaining steps (a
                # Gemini localize each) would just end in a misleading
                # "could not centre".
                raise SkillFailed("Base positioning failed (odometry lost or motion timed out)")
            px = None
        self._position_failed(prompt)
