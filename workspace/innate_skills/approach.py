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

from innate import vision
from innate.exceptions import SkillFailed
from innate.geometry import FX, FY, HEAD_ORIGIN, IMG_H, IMG_W, floor_to_pixel, pixel_to_floor

if TYPE_CHECKING:
    from innate import Llm, MainImage, Mobility, Odometry, Overlay

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

    llm: "Llm"
    mobility: "Mobility"
    main_image: "MainImage | None"
    odom: "Odometry | None"

    @property
    def logger(self) -> _Logger: ...
    @property
    def name(self) -> str: ...
    def sleep(self, seconds: float) -> None: ...
    def say(self, text: str, wait: bool = False) -> None: ...
    @property
    def overlay(self) -> "Overlay": ...


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


def ask_head(host: ApproachHost, question: str, settle_s: float):
    """Settle the base, then put a head frame taken after it to the host's model.
    -> (reply_text|None, frame|None)."""
    host.mobility.stop()
    img = settled_frame(host, settle_s)
    if not img:
        return None, None
    return host.llm.ask(img, question, logger=host.logger), img


def settled_frame(
    host: ApproachHost, settle_s: float, timeout: float = 0.6, read: "Callable[[], str | None] | None" = None
):
    """The first frame published after the settle sleep — one captured with
    the robot at rest, never one the pipeline still held from mid-motion.
    ``read`` is the feed (the head camera by default). Identity, not
    content: consecutive sim frames of a still scene are byte-identical."""
    read = read or (lambda: host.main_image)
    stale = read()
    host.sleep(settle_s)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        img = read()
        if img and img is not stale:
            return img
        host.sleep(0.03)
    return read()


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


def metres(m: float) -> str:
    return f"{m:.2f} m" if m >= 1 else f"{round(m * 100)} cm"


class FloorApproach:
    """Head-camera localize + base servo for one run of a hosting skill."""

    def __init__(self, host: ApproachHost, params: dict, detect: Detect):
        self.host = host
        self.p = params
        self.detect = detect
        self._local_tracker = None
        self._tracker_odom = None
        self._arrival_samples = []

    # --- localize ---

    def _localize_px(self, prompt):
        """Detect + back-project -> ((x,y)|None, pixel|None)."""
        px = self.detect(prompt)
        self._local_tracker = None
        if px is not None and self.p.get("local_visual_recovery", False):
            self._remember_visual(px)
        if px is None:
            return None, None
        xy = pixel_to_floor(px[0], px[1], self.p["tilt_deg"])
        if xy:
            self.host.logger.info(
                f"[{self.host.name}] px=({px[0]:.0f},{px[1]:.0f}) -> base_link ({xy[0]:.3f},{xy[1]:.3f})"
            )
        return xy, px

    def _remember_visual(self, px):
        from innate_skills.local_target import LocalTarget
        raw = getattr(self.host, "_local_detection_image", None)
        box = getattr(self.host, "_local_detection_box", None)
        gray = vision.b64_to_gray(raw) if raw else None
        if gray is not None and box is not None:
            self._local_tracker = LocalTarget(gray, px, box, edges_only=self.p.get("recovery_box_edges", False))
            self._tracker_odom = self.odom_xyt()

    def _recover_visual(self):
        """Stop and try two fresh visual matches within 0.45s; never drive blind."""
        self.host.mobility.stop()
        tracker = self._local_tracker
        if tracker is None:
            return None, None
        xy = pixel_to_floor(*tracker.anchor, self.p["tilt_deg"])
        world = base_to_odom(self._tracker_odom, xy) if xy is not None else None
        if world is None:
            return None, None
        deadline = time.monotonic() + .45
        raw = self.host.main_image
        accepted = []
        while time.monotonic() < deadline:
            self.host.sleep(.03)
            img = self.host.main_image
            if not img or img is raw:
                continue
            raw = img
            odom = self.odom_xyt()
            expected = odom_to_base(odom, world)
            predicted = floor_to_pixel(*expected, self.p["tilt_deg"]) if expected is not None else None
            if predicted is None:
                return None, None
            gray = vision.b64_to_gray(img)
            px = tracker.track(gray, predicted) if gray is not None else None
            measured = pixel_to_floor(*px, self.p["tilt_deg"]) if px is not None else None
            if measured is None or math.dist(measured, expected) > .045:
                accepted.clear()
                continue
            # Compare in world coordinates even if residual wheel motion exists.
            seen = base_to_odom(odom, measured)
            accepted.append(seen)
            if len(accepted) >= 2 and math.dist(*accepted[-2:]) <= .005:
                self._tracker_odom = odom
                self.host.logger.info(f"[{self.host.name}] local visual recovery; skipping model re-detection")
                return measured, px
        self._local_tracker = None
        self.host.logger.info(f"[{self.host.name}] local visual recovery uncertain; using model")
        return None, None

    def _localize_retry(self, prompt):
        """One retry: a single "not visible" is noise, not absence."""
        xy, px = self._localize_px(prompt)
        if px is None:
            xy, px = self._localize_px(prompt)
        return xy, px

    def search(self, prompt):
        """Scan: straight, right 30°, left 60°. First hit wins. (+yaw=left)"""
        self.host.overlay.stage("search")
        turns = self.p.get("search_turns_deg", (0, -30, 60))
        for i, degrees in enumerate(turns):
            turn = math.radians(degrees)
            if turn:
                if i == 1 and not self.p.get("silent_search", False):
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

    def _moving(self, what):
        """The picture is about to change under every head-camera marker."""
        self.host.overlay.clear(view="main")
        self.host.overlay.readout(what)

    def rotate_by(self, angle):
        self._moving(f"turning {round(abs(math.degrees(angle)))}° {'left' if angle >= 0 else 'right'}")
        if self.p.get("ease_base_motion", False):
            return self._eased_move(angle, turning=True)
        return self.host.mobility.rotate_by(
            self.odom_xyt,
            angle,
            kp=self.p["rot_kp"],
            wz_max=self.p["rot_wz_max"],
            wz_min=self.p["rot_wz_min"],
            tolerance=math.radians(self.p["rot_tol_deg"]),
            logger=self.host.logger,
        )

    def drive(self, dist, *, brisk=False):
        """``brisk`` holds top speed to the stop, for a move that only has to
        be roughly there; the default creeps in for a park."""
        self._moving(f"{'driving' if dist >= 0 else 'backing up'} {metres(abs(dist))}")
        if self.p.get("ease_base_motion", False):
            return self._eased_move(dist, turning=False)
        return self.host.mobility.drive(
            self.odom_xyt,
            dist,
            kp=self.p["drive_kp"],
            v_max=self.p["drive_v_max"],
            v_min=self.p["drive_v_max" if brisk else "drive_v_min"],
            tolerance=self.p["drive_tol_m"],
            logger=self.host.logger,
        )

    def _eased_move(self, amount, *, turning):
        """Odom-controlled move with gradual acceleration and a tapered finish.
        Lost feedback, cancellation and arrival brake immediately; ramps never
        delay a stop. No open-loop fallback for this faster demo path.
        """
        tolerance = math.radians(self.p["rot_tol_deg"]) if turning else self.p["drive_tol_m"]
        if abs(amount) <= tolerance:
            self.host.mobility.stop()
            return True
        start = self.odom_xyt()
        if start is None:
            self.host.mobility.stop()
            return False
        ramp = ApproachRamp()
        began = time.monotonic()
        try:
            while time.monotonic() - began < (12.0 if turning else 15.0):
                now = self.odom_xyt()
                if now is None:
                    return False
                if turning:
                    err = math.atan2(math.sin(start[2] + amount - now[2]),
                                     math.cos(start[2] + amount - now[2]))
                    kp, top, decel = self.p["rot_kp"], self.p["rot_wz_max"], 1.5
                else:
                    travelled = ((now[0]-start[0])*math.cos(start[2])
                                 + (now[1]-start[1])*math.sin(start[2]))
                    err = amount - travelled
                    kp, top, decel = self.p["drive_kp"], self.p["drive_v_max"], 0.35
                if abs(err) <= tolerance:
                    return True
                # A braking envelope avoids asking the ramp to stop abruptly
                # from cruising speed right at the arrival threshold.
                speed = math.copysign(min(top, max(0.025 if not turning else 0.08, kp*abs(err)),
                                         math.sqrt(2*decel*max(0.0,abs(err)-tolerance))), err)
                vx, wz = ramp.step(0.0 if turning else speed, speed if turning else 0.0)
                self.host.mobility.send_cmd_vel(vx, wz, 0.15)
                self.host.sleep(0.03)
            self.host.logger.warning(f"[{self.host.name}] eased move timed out")
            return False
        finally:
            self.host.mobility.stop()

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

    def _draw_track(self, px, cu, cv, inside):
        ui = self.host.overlay
        hu, hv = self.p["box_half_px"], self.p["box_half_v_px"]
        ui.box(
            "pick-box",
            (cu - hu, cv - hv, cu + hu, cv + hv),
            inner=self.p["accept_frac"],
            label="pick box",
            locked=inside,
        )
        ui.point("track", px, label="tracking", locked=inside)
        if inside:
            ui.clear("steer")
            ui.readout("in the pick box")
        else:
            ui.vector("steer", px, (cu, cv))
            ui.readout("steering onto it")

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
        self._arrival_samples = []
        (cu, cv), _half, accept = self._sweet_box()
        self.host.overlay.clear("target")
        t0 = time.monotonic()
        anchor, anchor_odo = (u, v), self.odom_xyt()
        seg_start = anchor_odo
        ramp = ApproachRamp() if self.p.get("ease_base_motion", False) else None
        target_vx = target_wz = 0.0
        last_frame_at = time.monotonic()
        last_motion_log = t0 - 1.0
        while time.monotonic() - t0 < FOLLOW_TIMEOUT_S:
            # Only track NEW frames: the camera runs slower than this loop,
            # and a stale frame re-tracked would count one observation twice.
            # Compare by identity, not content: the provider builds one Image
            # per ROS message, and in sim consecutive frames of a static scene
            # are byte-identical, so `==` would deadlock waiting for a change.
            img = self.host.main_image
            if not img or img is raw:
                if ramp is not None:
                    if time.monotonic() - last_frame_at > 0.30:
                        self.host.mobility.stop()
                        return "noframe", None
                    vx, wz = ramp.step(target_vx, target_wz)
                    self.host.mobility.send_cmd_vel(vx, wz, 0.15)
                self.host.sleep(0.03)
                continue
            gray = vision.b64_to_gray(img)
            raw = img
            last_frame_at = time.monotonic()
            if gray is None:
                if self.p.get("local_visual_recovery", False):
                    self.host.mobility.stop()
                    return "noframe", None
                self.host.sleep(0.03)
                continue
            if self.p.get("local_visual_recovery", False):
                if self.odom_xyt() is None:
                    self.host.mobility.stop()
                    return "lost", None
                tracked = self._local_tracker.track(gray) if self._local_tracker is not None else None
                if tracked is None:
                    _xy, tracked = self._recover_visual()
                    if tracked is not None:
                        raw = self.host.main_image
                        gray = self._local_tracker.gray
                        last_frame_at = time.monotonic()
                        target_vx = target_wz = 0.0
                        ramp = ApproachRamp() if ramp is not None else None
                else:
                    self._tracker_odom = self.odom_xyt()
            elif self.p.get("robust_floor_tracking", False):
                tracked = track_floor_anchor(prev, gray, grid, (u, v))
            else:
                tracked = vision.track_point(prev, gray, grid)
            prev = gray
            if tracked is None:
                if self.p.get("robust_floor_tracking", False):
                    self.host.logger.info(f"[{self.host.name}] tracking fallback: insufficient consistent features")
                self.host.mobility.stop()
                return "lost", None
            u, v = tracked
            grid = vision.grid_pts(u, v)
            if not (0 <= u < IMG_W and 0 <= v < IMG_H):
                self.host.mobility.stop()
                return "lost", None

            floor_est = pixel_to_floor(u, v, self.p["tilt_deg"])
            metric = self.p.get("metric_sock_approach", False)
            if metric and (floor_est is None or not all(math.isfinite(q) for q in floor_est)):
                self.host.mobility.stop()
                return "lost", None
            inside = (sock_arrived(floor_est, self.p["sweet_x"]) if metric
                      else inside_box((u, v), cu, cv, accept[0], accept[1]))
            self._draw_track((u, v), cu, cv, inside)
            if inside:
                in_box += 1
                self._arrival_samples.append(pixel_to_floor(u, v, self.p["tilt_deg"]))
                self.host.mobility.stop()
                target_vx = target_wz = 0.0
                if ramp is not None:
                    ramp = ApproachRamp()
                anchor, anchor_odo = (u, v), self.odom_xyt()
                if in_box >= 3:
                    return "in_box", (u, v)
                self.host.sleep(0.03)
                continue
            in_box = 0
            self._arrival_samples = []

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
            # Coarse forward travel can be faster; retain the original final
            # positioning speeds within 10 cm or when the target is off-axis.
            gain, v_min, v_max = approach_linear_limits(self.p, floor_est, u - cu)
            vx = (sock_approach_speed(floor_est, self.p["sweet_x"]) if metric else
                  self.host.mobility.servo_vel(v - cv, gain, v_min, v_max, accept[1]))
            if metric:
                wz = sock_approach_turn(floor_est, self.p["sweet_x"])
            if ramp is not None:
                # Ease out before entering the image deadband. Keep enough
                # speed to overcome wheel friction until the arrival check.
                if vx and not metric:
                    vx = math.copysign(max(0.025, abs(vx)*min(1.0, max(0.0, (abs(v-cv)-accept[1])/40.0))), vx)
                if wz and not metric:
                    wz = math.copysign(max(0.08, abs(wz)*min(1.0, max(0.0, (abs(u-cu)-accept[0])/40.0))), wz)
                target_vx, target_wz = vx, wz
                vx, wz = ramp.step(vx, wz)
            if metric and time.monotonic() - last_motion_log >= 0.5:
                self.host.logger.info(
                    f"[{self.host.name}] sock approach xy=({floor_est[0]:.3f},{floor_est[1]:+.3f}) "
                    f"velocity=({vx:.3f}m/s,{wz:+.3f}rad/s)"
                )
                last_motion_log = time.monotonic()
            self.host.mobility.send_cmd_vel(vx, wz, 0.15)
            self.host.sleep(0.03)
        self.host.mobility.stop()
        return "timeout", None

    def _position_failed(self, prompt):
        raise SkillFailed(f"Could not centre '{prompt}' in the approach box")

    def position_above(self, prompt, xy):
        """Flow-follow into the sweet box; Gemini reseed/confirm. Stepwise if
        no cam. Raises SkillFailed if the target cannot be centred."""
        self.host.overlay.stage("approach")
        if not self.host.main_image:
            return self._position_stepwise(prompt, xy)

        # Centre first: a seed near a frame edge can land on the arm or the
        # carried object, and the servo would chase the robot itself.
        bearing = math.atan2(xy[1], xy[0])
        if abs(bearing) > math.radians(self.p["bearing_go_deg"]):
            self.rotate_by(bearing)
            xy2, px2 = self._recover_visual() if self.p.get("local_visual_recovery", False) else (None, None)
            if px2 is None:
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
                target_odo = base_to_odom(self.odom_xyt(), xy) if xy is not None else None
            # Arrival checked BEFORE servoing: a tracker that dies on a
            # parked base must not burn the step budget re-seeding.
            (cu, cv), hold, _accept = self._sweet_box()
            if xy is not None and (sock_arrived(xy, self.p["sweet_x"]) if self.p.get("metric_sock_approach", False)
                                   else inside_box(seed, cu, cv, hold[0], hold[1])):
                return xy
            rem = _remaining()
            if rem is not None and rem[0] <= stop_x:
                return _parked(rem)
            result, _pt = self._follow_into_box(seed, max_forward=(rem[0] - stop_x) if rem is not None else None)
            if self.p.get("accept_tracked_arrival", False):
                self.host.logger.info(f"[{self.host.name}] tracking result={result}, pixel={_pt}")
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
            # Opt-in for a large flat target: accept fresh flow only when its
            # floor projection agrees with the odometry-anchored target.
            if self.p.get("accept_tracked_arrival", False) and result == "in_box" and _pt is not None:
                tracked = pixel_to_floor(_pt[0], _pt[1], self.p["tilt_deg"])
                rem = _remaining()
                samples = getattr(self, "_arrival_samples", [])
                stable = (self.p.get("stable_arrival_margin", False) and len(samples) >= 3
                          and all(s is not None for s in samples[-3:])
                          and max(math.dist(a, b) for a in samples[-3:] for b in samples[-3:]) <= self.p.get("arrival_tracking_spread_m", .005))
                if tracked_arrival_ok(
                    tracked, rem, self.p["sweet_x"], stable=stable,
                    trust_tracking=self.p.get("trust_sock_tracking", False),
                ):
                    self.host.mobility.stop()
                    self.host.logger.info(f"[{self.host.name}] tracked arrival; skipping model re-confirmation")
                    return tracked
                self.host.logger.info(
                    f"[{self.host.name}] tracking confirmation required: "
                    f"tracked={tracked}, odometry={rem}, sweet_x={self.p['sweet_x']}, "
                    "limits: range=0.04m, lateral=0.05m, agreement=0.04m"
                )
            # The flow servo already parked this within `accept`; re-demanding
            # that of a Gemini box edge only re-servos on detector noise.
            xy2, px2 = self._localize_retry(prompt)
            if px2 is None:
                self._position_failed(prompt)
            if xy2 is not None and (sock_arrived(xy2, self.p["sweet_x"]) if self.p.get("metric_sock_approach", False)
                                    else inside_box(px2, cu, cv, hold[0], hold[1])):
                return xy2
            xy, seed = xy2, (px2 if xy2 is not None else None)
            target_odo = base_to_odom(self.odom_xyt(), xy) if xy is not None else None
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


def tracked_arrival_ok(tracked, remaining, sweet_x, *, stable=False, trust_tracking=False):
    """Sock opt-in trusts stable visual arrival; other skills require odometry."""
    if tracked is None or not all(math.isfinite(v) for v in tracked):
        return False
    if not (abs(tracked[0] - sweet_x) <= 0.04 and abs(tracked[1]) <= 0.05):
        return False
    if trust_tracking:
        # Stability comes from three fresh arrival frames, not repeated reads
        # of one image. The normal pickup reachability check still follows.
        return stable
    if remaining is None or not all(math.isfinite(v) for v in remaining):
        return False
    return math.dist(tracked, remaining) <= (0.045 if stable else 0.04)


def track_floor_anchor(prev_gray, gray, grid, anchor):
    """Track the requested box-floor contact, not the surviving patch centre.

    With partial occlusion, median destination pixels shifts the target toward
    whichever grid features survive. Median displacement preserves the original
    contact point. Forward/backward consistency rejects mismatched features.
    """
    import cv2
    import numpy as np

    nxt, status, _ = cv2.calcOpticalFlowPyrLK(prev_gray, gray, grid, None, **vision.LK_PARAMS)
    if nxt is None or status is None:
        return None
    valid = status.reshape(-1) == 1
    src = grid.reshape(-1, 2)[valid]
    dst = nxt.reshape(-1, 2)[valid]
    finite = np.isfinite(src).all(axis=1) & np.isfinite(dst).all(axis=1)
    src, dst = src[finite], dst[finite]
    if len(src) < 8:
        return None
    back, back_status, _ = cv2.calcOpticalFlowPyrLK(
        gray, prev_gray, dst.reshape(-1, 1, 2), None, **vision.LK_PARAMS)
    if back is None or back_status is None:
        return None
    good = ((back_status.reshape(-1) == 1)
            & (np.linalg.norm(back.reshape(-1, 2) - src, axis=1) <= 2.0))
    shifts = (dst - src)[good]
    if len(shifts) < 8:
        return None
    median = np.median(shifts, axis=0)
    consistent = shifts[np.linalg.norm(shifts - median, axis=1) <= 3.0]
    if len(consistent) < 8:
        return None
    dx, dy = np.median(consistent, axis=0)
    return float(anchor[0] + dx), float(anchor[1] + dy)


def sock_arrived(xy, sweet_x):
    return (xy is not None and all(math.isfinite(v) for v in xy)
            and abs(xy[0] - sweet_x) <= 0.02 and abs(xy[1]) <= 0.03)


def sock_approach_speed(xy, sweet_x):
    """Distance-based braking, including the existing ramp's 0.4 s lag.

    Solve v²/(2a) + lag*v = remaining travel before the arrival boundary.
    Acceleration remains limited by ApproachRamp, unchanged for all skills.
    """
    if xy is None or not all(math.isfinite(v) for v in xy):
        return 0.0
    error = xy[0] - sweet_x
    if abs(error) <= 0.02:
        return 0.0
    distance = max(0.0, abs(error) - 0.015)
    a, lag = 0.2, 0.4
    speed = min(0.18, math.sqrt((a * lag)**2 + 2*a*distance) - a*lag)
    # Like FollowAruco, translate and steer together. A fixed sideways
    # offset is harmless far away: reduce speed by bearing, not centimetres.
    heading = abs(math.atan2(xy[1], xy[0]))
    speed *= math.cos(1.5 * heading) ** 2 if heading < math.pi / 3 else 0.0
    if error < 0:
        speed = min(speed, 0.06)
    return math.copysign(speed, error)


def sock_approach_turn(xy, sweet_x):
    """Proportional steering, without the old minimum-speed turn kicks.

    Coarse alignment while distant; tighten the angular deadband only near
    pickup. Forward motion continues during small corrections, as in FollowAruco.
    """
    if xy is None or not all(math.isfinite(v) for v in xy):
        return 0.0
    heading = math.atan2(xy[1], xy[0])
    near = xy[0] <= sweet_x + 0.15
    deadband = math.atan2(0.015, max(xy[0], 0.1)) if near else math.radians(3)
    error = max(0.0, abs(heading) - deadband)
    return math.copysign(min(0.8, 1.5 * error), heading)


def approach_linear_limits(p, floor_xy, lateral_error_px):
    if (p.get("fast_far_approach", False)
            and floor_xy[0] > p["sweet_x"] + 0.10
            and abs(lateral_error_px) <= 2 * p["box_half_px"]):
        return p.get("far_gain_lin", 0.14), 0.08, 0.18
    return p["follow_gain_lin"], p["drive_v_min"], p["drive_v_max"]


class ApproachRamp:
    """Time-based ease-in/out for changing visual targets; independent of fps."""

    def __init__(self):
        self.vx = self.wz = 0.0
        self.at = time.monotonic()

    def step(self, vx, wz):
        now = time.monotonic()
        dt = max(0.0, min(0.06, now - self.at))
        self.at = now
        # Match the app's verified normal forward handling (.2 m/s², .4s).
        # Turning is deliberately gentler than the app's 2 rad/s² / .1s.
        for name, target, acceleration, tau in (("vx", vx, 0.2, 0.4), ("wz", wz, 0.8, 0.2)):
            current = getattr(self, name)
            # Come through zero before reversing a steering correction.
            if current * target < 0:
                target = 0.0
            # Gentle turn-in must not imply a long turn-out tail. The app
            # also separates these limits (2 accel / 6 decel rad/s²).
            if name == "wz" and abs(target) < abs(current):
                acceleration, tau = 2.0, 0.08
            alpha = 1.0 - math.exp(-dt / tau)
            change = (target-current)*alpha
            change = max(-acceleration*dt, min(acceleration*dt, change))
            value = current + change
            if abs(value) < 0.001 and target == 0.0:
                value = 0.0
            setattr(self, name, value)
        return self.vx, self.wz
