# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Find a thing on the floor and drive the base until it sits in a chosen spot.
A collaborator, not a base class: a skill builds one per run with itself as the
host, its params (where to park) and a detect callable (which pixel of the
target touches the floor), so pick and drop share one hardware-tuned approach.
"""

import math
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from innate import gemini as gemlib
from innate import vision
from innate.exceptions import SkillFailed
from innate.geometry import FX, FY, HEAD_ORIGIN, IMG_H, IMG_W, floor_to_pixel, pixel_to_floor

if TYPE_CHECKING:
    from innate import MainImage, Mobility, Odometry
    from innate_proxy import ProxyClient

Pixel = tuple[float, float]
FloorXY = tuple[float, float]
Detect = Callable[[str], "Pixel | None"]
OdomXYT = tuple[float, float, float]


class _Logger(Protocol):
    def info(self, msg: str) -> None: ...
    def warning(self, msg: str) -> None: ...


class ApproachHost(Protocol):
    """What a skill offers to host a FloorApproach: the feeds it declares and
    the cancel-aware waits only a Skill can provide."""

    mobility: "Mobility"
    main_image: "MainImage | None"
    odom: "Odometry | None"

    @property
    def logger(self) -> _Logger: ...
    @property
    def name(self) -> str: ...
    def sleep(self, seconds: float) -> None: ...
    def say(self, text: str, wait: bool = False) -> None: ...


APPROACH_PARAMS = {
    "tilt_deg": -20.0,
    "settle_s": 1.2,
    # 0.285 = the 0.37 pick was tuned to, renumbered by the 2026-08-28 camera
    # calibration (the old model read ranges ~2x long); same pixel target.
    "sweet_x": 0.285,
    "box_y": 0.0,
    "box_half_px": 40.0,
    "box_half_v_px": 40.0,
    "accept_frac": 0.5,
    # Equal to accept_frac, so by default the two boxes coincide and a skill
    # tests one box exactly as it did before `hold` existed. Pick keeps that:
    # its ±20 px park is the grasp capture window, and a looser hold band
    # would let a Gemini re-read certify a park the gripper cannot reach from.
    "hold_frac": 0.5,
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
# How far past the park the dead-reckoned target may come before the drive
# stops: a target filling the frame reads a stable "2 cm short" even with the
# bumper against it, so only odometry can bound the endgame.
TRAVEL_MARGIN_M = 0.08
# The same wall bias plateaus the final reading ~2 cm outside the accept band;
# within this slack the run is parked and the reach clamp absorbs it.
PLATEAU_SLACK_M = 0.05
# A pixel glued in place through real base motion is on the robot (or a pushed
# box), not the floor: static = under a third of _min_px_shift's minimum.
STATIC_MIN_PX = 5.0


def _min_px_shift(o0, o1, floor_xy):
    """Least pixel travel a floor point must show between two odometry poses;
    0 without odometry. FY*h/x^2 over-reads ~1.6x up close — deliberately: the
    exact gradient measured 0/6 picks against this formula's 3/6."""
    if o0 is None or o1 is None:
        return 0.0
    dyaw = abs(math.atan2(math.sin(o1[2] - o0[2]), math.cos(o1[2] - o0[2])))
    dist = math.hypot(o1[0] - o0[0], o1[1] - o0[1])
    fwd = dist * FY * HEAD_ORIGIN[2] / max(floor_xy[0], 0.15) ** 2 if floor_xy else 0.0
    return dyaw * FX + fwd


def ask_head(host: ApproachHost, proxy: "ProxyClient | None", question: str, settle_s: float):
    """Settle the base, then put the current head frame to Gemini.
    -> (reply_text|None, frame|None)."""
    host.mobility.stop()
    host.sleep(settle_s)
    img = host.main_image
    if not img:
        return None, None
    return gemlib.ask_image(proxy, img, question, logger=host.logger), img


def base_to_odom(o: "OdomXYT | None", xy: FloorXY) -> "FloorXY | None":
    """base_link floor point -> odom frame, or None without odometry."""
    if o is None:
        return None
    ox, oy, th = o
    c, s = math.cos(th), math.sin(th)
    return (ox + c * xy[0] - s * xy[1], oy + s * xy[0] + c * xy[1])


def odom_to_base(o: "OdomXYT | None", wxy: FloorXY) -> "FloorXY | None":
    """odom-frame floor point -> current base_link, or None without odometry."""
    if o is None:
        return None
    ox, oy, th = o
    c, s = math.cos(th), math.sin(th)
    dx, dy = wxy[0] - ox, wxy[1] - oy
    return (c * dx + s * dy, -s * dx + c * dy)


def inside_box(px, cu, cv, half_u, half_v=None):
    return abs(px[0] - cu) <= half_u and abs(px[1] - cv) <= (half_u if half_v is None else half_v)


class FloorApproach:
    """Head-camera localize + base servo for one run of a hosting skill."""

    def __init__(self, host: ApproachHost, params: dict, detect: Detect, *, confirm_arrival: bool = True):
        self.host = host
        self.p = params
        self.detect = detect
        self.confirm_arrival = confirm_arrival

    # --- localize ---

    def _localize_px(self, prompt):
        """Detect + back-project -> ((x,y)|None, pixel|None)."""
        px = self.detect(prompt)
        if px is None:
            return None, None
        xy = pixel_to_floor(px[0], px[1], self.p["tilt_deg"])
        if xy:
            self.host.logger.info(
                f"[{self.host.name}] px=({px[0]:.0f},{px[1]:.0f}) -> base_link ({xy[0]:.3f},{xy[1]:.3f})"
            )
        return xy, px

    def _localize_retry(self, prompt):
        """One retry: a single "not visible" is noise, not absence."""
        xy, px = self._localize_px(prompt)
        if px is None:
            xy, px = self._localize_px(prompt)
        return xy, px

    def search(self, prompt):
        """Scan: straight, right 30°, left 60°. First hit wins. (+yaw=left)"""
        for i, turn in enumerate((0.0, -math.radians(30), math.radians(60))):
            if turn:
                if i == 1:
                    self.host.say("Scanning around for it.")
                # Best-effort: a rotate cut short (timeout / odom loss) still
                # changed the view, and the localize below measures from
                # wherever the base actually ended up.
                self.rotate_by(turn)
            xy, _px = self._localize_px(prompt)
            if xy is not None:
                return xy
        raise SkillFailed(f"Could not find '{prompt}' on the floor, even after scanning")

    # --- base motion ---

    def odom_xyt(self):
        return self.host.mobility.odom_xyt(self.host.odom)

    def rotate_by(self, angle):
        return self.host.mobility.rotate_by(
            self.odom_xyt,
            angle,
            kp=self.p["rot_kp"],
            wz_max=self.p["rot_wz_max"],
            wz_min=self.p["rot_wz_min"],
            tolerance=math.radians(self.p["rot_tol_deg"]),
            logger=self.host.logger,
        )

    def drive(self, dist):
        return self.host.mobility.drive(
            self.odom_xyt,
            dist,
            kp=self.p["drive_kp"],
            v_max=self.p["drive_v_max"],
            v_min=self.p["drive_v_min"],
            tolerance=self.p["drive_tol_m"],
            logger=self.host.logger,
        )

    # --- position the base ---

    def _sweet_box(self):
        """(centre, (hold_u, hold_v), (accept_u, accept_v)). Two boxes because
        two things measure the park: `accept` is the flow servo's deadband,
        tight because a tracked pixel is precise; `hold` is what a Gemini
        re-read must fall inside to count as parked, loose because a detector
        whose box edge jitters tens of pixels cannot resolve millimetres.
        Two axes as well: near the park one image row is ~cm of RANGE but
        sub-mm of bearing, so one tolerance cannot serve both."""
        c = floor_to_pixel(self.p["sweet_x"], self.p["box_y"], self.p["tilt_deg"])
        if c is None or not (0 <= c[0] < IMG_W and 0 <= c[1] < IMG_H):
            raise SkillFailed("approach box off-image — check tilt_deg/sweet_x")
        hu, hv = self.p["box_half_px"], self.p["box_half_v_px"]
        hold, accept = self.p["hold_frac"], self.p["accept_frac"]
        return (c[0], c[1]), (hu * hold, hv * hold), (hu * accept, hv * accept)

    def _follow_into_box(self, seed_px, max_forward=None):
        """Optical-flow base servo into the sweet box. No Gemini.
        Returns ('in_box'|'lost'|'timeout'|'noframe'|'budget', px|None);
        'budget' means max_forward metres of odometry were spent."""
        raw = self.host.main_image
        prev = vision.b64_to_gray(raw) if raw else None
        if prev is None:
            return "noframe", None
        u, v = seed_px
        grid = vision.grid_pts(u, v)
        in_box = 0
        (cu, cv), _half, accept = self._sweet_box()
        t0 = time.monotonic()
        anchor, anchor_odo = (u, v), self.odom_xyt()
        seg_start = anchor_odo
        while time.monotonic() - t0 < FOLLOW_TIMEOUT_S:
            # Only track NEW frames: the camera runs slower than this loop,
            # and a stale frame re-tracked would count one observation twice.
            # Compare by identity, not content: the provider builds one Image
            # per ROS message, and in sim consecutive frames of a static scene
            # are byte-identical, so `==` would deadlock waiting for a change.
            img = self.host.main_image
            if not img or img is raw:
                self.host.sleep(0.03)
                continue
            gray = vision.b64_to_gray(img)
            raw = img
            if gray is None:
                self.host.sleep(0.03)
                continue
            tracked = vision.track_point(prev, gray, grid)
            prev = gray
            if tracked is None:
                self.host.mobility.stop()
                return "lost", None
            u, v = tracked
            grid = vision.grid_pts(u, v)
            if not (0 <= u < IMG_W and 0 <= v < IMG_H):
                self.host.mobility.stop()
                return "lost", None

            if inside_box((u, v), cu, cv, accept[0], accept[1]):
                in_box += 1
                self.host.mobility.stop()
                anchor, anchor_odo = (u, v), self.odom_xyt()
                if in_box >= 3:
                    return "in_box", (u, v)
                self.host.sleep(0.03)
                continue
            in_box = 0

            floor_est = pixel_to_floor(u, v, self.p["tilt_deg"])
            if floor_est is None:
                # Above the horizon: not a floor point any more.
                self.host.mobility.stop()
                return "lost", None
            if math.hypot(u - anchor[0], v - anchor[1]) >= STATIC_MIN_PX:
                anchor, anchor_odo = (u, v), self.odom_xyt()
            elif _min_px_shift(anchor_odo, self.odom_xyt(), floor_est) >= 3 * STATIC_MIN_PX:
                self.host.mobility.stop()
                return "lost", None

            now_odo = self.odom_xyt()
            if (
                max_forward is not None
                and seg_start is not None
                and now_odo is not None
                and math.hypot(now_odo[0] - seg_start[0], now_odo[1] - seg_start[1]) >= max_forward
            ):
                self.host.mobility.stop()
                return "budget", (u, v)

            # Deadband = accept (inner) box; right -> -wz, too close (low) -> -vx.
            wz = self.host.mobility.servo_vel(
                u - cu, self.p["follow_gain_ang"], self.p["rot_wz_min"], self.p["rot_wz_max"], accept[0]
            )
            vx = self.host.mobility.servo_vel(
                v - cv, self.p["follow_gain_lin"], self.p["drive_v_min"], self.p["drive_v_max"], accept[1]
            )
            self.host.mobility.send_cmd_vel(vx, wz, 0.15)
            self.host.sleep(0.03)
        self.host.mobility.stop()
        return "timeout", None

    def _position_failed(self, prompt):
        raise SkillFailed(f"Could not centre '{prompt}' in the approach box")

    def position_above(self, prompt, xy):
        """Flow-follow into the sweet box; Gemini reseed/confirm. Stepwise if
        no cam. Raises SkillFailed if the target cannot be centred."""
        if not self.host.main_image:
            return self._position_stepwise(prompt, xy)

        # Centre first: a seed near a frame edge can land on the arm or the
        # carried object, and the servo would chase the robot itself.
        bearing = math.atan2(xy[1], xy[0])
        if abs(bearing) > math.radians(self.p["bearing_go_deg"]):
            self.rotate_by(bearing)
            xy2, px2 = self._localize_retry(prompt)
            if px2 is None:
                self._position_failed(prompt)
            if xy2 is None:
                # Keeping the pre-rotation xy would pin target_odo below against
                # the POST-rotation pose, putting the world target a whole turn
                # off — and the base would dead-reckon to it.
                raise SkillFailed(
                    f"Turned toward '{prompt}', but it no longer back-projects onto the "
                    "floor ahead — it does not look like something resting on the floor"
                )
            xy = xy2
            seed = px2
        else:
            seed = floor_to_pixel(xy[0], xy[1], self.p["tilt_deg"])
        lost = 0
        # The target pinned in the odom frame by the first honest measurement;
        # transformed back per iteration it stays frame-correct however the
        # base curves, where "range minus displacement" only holds for a line.
        target_odo = base_to_odom(self.odom_xyt(), xy)
        stop_x = self.p["sweet_x"] - TRAVEL_MARGIN_M

        def _remaining():
            return odom_to_base(self.odom_xyt(), target_odo) if target_odo is not None else None

        def _current_xy():
            # The target in the CURRENT base frame: a measurement taken before
            # the servo moved the base is in an obsolete one, so with neither
            # odometry nor a fresh look there is nothing honest to drive toward.
            rem = _remaining()
            if rem is not None:
                return rem
            fresh, _px = self._localize_retry(prompt)
            if fresh is None:
                raise SkillFailed(f"Lost '{prompt}' with neither camera nor odometry to relocate it")
            return fresh

        def _parked(rem):
            # Dead-reckoned, not re-measured: the wall illusion that spent the
            # budget reads ~0.25 m regardless of the truth.
            self.host.mobility.stop()
            near, y = max(0.10, min(0.60, rem[0])), max(-0.15, min(0.15, rem[1]))
            self.host.logger.info(
                f"[{self.host.name}] odometry budget spent: dead-reckoned park at ({near:.2f}, {y:+.2f})"
            )
            return (near, y)

        for _attempt in range(int(self.p["box_steps"])):
            if seed is None:
                xy, seed = self._localize_retry(prompt)
                if seed is None:
                    self._position_failed(prompt)
            # Arrival checked BEFORE servoing: a tracker that dies on a
            # parked base must not burn the step budget re-seeding.
            (cu, cv), hold, _accept = self._sweet_box()
            if xy is not None and inside_box(seed, cu, cv, hold[0], hold[1]):
                return xy
            rem = _remaining()
            if rem is not None and rem[0] <= stop_x:
                return _parked(rem)
            result, _pt = self._follow_into_box(seed, max_forward=(rem[0] - stop_x) if rem is not None else None)
            if result == "budget":
                rem = _remaining()
                if rem is not None:
                    return _parked(rem)
                seed = None
                continue
            if result == "noframe":
                return self._position_stepwise(prompt, _current_xy())
            if result == "lost":
                # Two losses running: flow has nothing to hold on a big
                # low-contrast face; the stepper closes on odometry instead.
                lost += 1
                if lost >= 2:
                    return self._position_stepwise(prompt, _current_xy())
                seed = None
                continue
            lost = 0
            if result == "in_box" and not self.confirm_arrival and _pt is not None:
                # Three fresh flow frames already confirmed arrival. A caller
                # with mandatory subsequent target recognition can use that
                # measured point instead of asking the head model again.
                arrived = pixel_to_floor(_pt[0], _pt[1], self.p["tilt_deg"])
                if arrived is not None:
                    return arrived
            # The flow servo already parked this within `accept`; re-demanding
            # that of a Gemini box edge only re-servos on detector noise.
            xy2, px2 = self._localize_retry(prompt)
            if px2 is None:
                self._position_failed(prompt)
            if xy2 is not None and inside_box(px2, cu, cv, hold[0], hold[1]):
                return xy2
            xy, seed = xy2, (px2 if xy2 is not None else None)
        if xy is not None and xy[0] <= self.p["sweet_x"] + PLATEAU_SLACK_M:
            self.host.mobility.stop()
            self.host.logger.info(f"[{self.host.name}] settling for the measured park at {xy[0]:.3f} m")
            return xy
        self._position_failed(prompt)

    def _position_stepwise(self, prompt, xy):
        """No-camera fallback: turn OR drive, re-detect, repeat.
        Raises SkillFailed if the target cannot be centred."""
        target_bearing = math.atan2(self.p["box_y"], self.p["sweet_x"])
        target_range = math.hypot(self.p["sweet_x"], self.p["box_y"])
        px = floor_to_pixel(xy[0], xy[1], self.p["tilt_deg"])
        for _step in range(int(self.p["box_steps"])):
            if px is None:
                xy, px = self._localize_retry(prompt)
                if px is None:
                    self._position_failed(prompt)
            (cu, cv), hold, _accept = self._sweet_box()
            if xy is not None and inside_box(px, cu, cv, hold[0], hold[1]):
                return xy
            if xy is None:
                px = None
                continue
            bearing_err = math.atan2(xy[1], xy[0]) - target_bearing
            if abs(bearing_err) > math.radians(self.p["bearing_go_deg"]):
                moved = self.rotate_by(bearing_err)
            else:
                moved = self.drive(math.hypot(xy[0], xy[1]) - target_range)
            if not moved:
                # Odom loss or a stuck base: this closed-odometry stepper
                # cannot make progress, so burning the remaining steps (a
                # Gemini localize each) would just end in a misleading
                # "could not centre".
                raise SkillFailed("Base positioning failed (odometry lost or motion timed out)")
            px = None
        self._position_failed(prompt)
