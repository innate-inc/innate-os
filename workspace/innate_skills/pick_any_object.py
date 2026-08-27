# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Pick an object from the floor by text prompt.

Metric localize (pixel -> floor -> base_link), drive into a pick box,
grasp (optional wrist visual-servo), verify by backing up.
No depth camera — URDF + pinhole model.
"""

import math
import re
import time

from innate_skills.approach import APPROACH_PARAMS, _FloorApproach, inside_box

from innate import (
    Head,
    JointStates,
    Manipulation,
    SkillReturn,
    Waypoint,
    WristImage,
    vision,
)
from innate import gemini as gemlib
from innate.exceptions import ArmFailed, ArmUnhealthy, SkillFailed
from innate.geometry import pixel_to_floor

GRIPPER_EMPTY_J6 = -0.085
VERIFY_BACKUP_M = 0.15
# Clamp for Gemini's per-object grip_strength. Grip force lives in hardware
# (current cap); this only serves as the soft/rigid signal for the fabric twist.
GRIP_STRENGTH_RANGE = (0.30, 0.60)
SOFT_GRIP_MIN = 0.5

# Post-pick carry pose (j1-5). j6 comes from close_strength, not this pose.
# j2 = -0.50: the coupling limit at j1~0.05 clamps past it and logs forever.
CARRY_ARM = [0.0537, -0.50, 0.4157, 0.9434, -0.0077]

# Search/approach pose (j1-5). REST's j4 = -0.3 pitches the gripper UP into the
# head camera, over the very floor patch the object sits on; the same fold with
# the wrist flattened and rolled clears the frame.
NAV_ARM = [1.5708, -1.2195, 1.5723, 0.06, -0.47]

# Pick parameters, tuned on hardware. The find/position half lives in
# APPROACH_PARAMS; sweet_x's ceiling is 0.43 (reach clamp) and 0.37 grasps at
# ~0.32 — grasping at the 0.40 reach edge stalls the wrist and overloads servo 2.
PARAMS = {
    **APPROACH_PARAMS,
    # Objects don't teleport: a match farther than this from the last sighting
    # is a different instance (e.g. an identical twin), not the target. The
    # allowance grows with the range the memory was taken at, because
    # pixel_to_floor over-ranges an object whose visible centre sits above the
    # floor by ~range * height / camera_height (0.26 m) — a 6 cm object at 2 m
    # back-projects 0.6 m too far, and only 0.11 m too far at the 0.37 m pick
    # range, so a flat gate would reject the very object it is protecting.
    "mem_gate_m": 0.35,
    "mem_gate_frac": 0.4,
    # WRIST ALIGN (0 wrist_steps = blind grasp)
    "wrist_steps": 2.0,
    "wrist_stop_z": 0.05,
    "wrist_z_step": 0.01,
    "wrist_move_s": 0.5,
    "wrist_pitch": 0.82,
    "wrist_box_u": 320.0,
    # Below image center: the wrist cam sits above the fingertips, so
    # mid-frame aims short of them. 350 is the hardware-tuned parallax bias.
    "wrist_box_v": 350.0,
    "wrist_half_px": 60.0,
    "wrist_kx": -0.04,
    "wrist_ky": -0.04,
    "wrist_step_max": 0.04,
    "wrist_settle_s": 0.8,
    # GRASP
    "grasp_x_off": 0.05,
    "hover_z": 0.15,
    "hover_s": 2.0,
    "descend_z1": 0.10,
    "descend_z2": 0.07,
    "descend_z3": 0.045,
    # ee_link target, not fingertip height. 0.01 dug into carpet and aborted.
    "floor_z": 0.03,
    "descend_s": 1.2,
    "descend_abort_z": 0.12,
    "arm_pitch": 1.30,
    # close_strength is close depth, not force (servo 6 runs current-based
    # position control); above 0.6 the servo trips and needs a reboot.
    "close_strength": 0.60,
    "close_s": 1.5,
    "close_settle_s": 0.8,
    # Un-press before closing: the descent parks the fingers pressed into the
    # floor; a small lift lets them close around the object, not drag it.
    "close_lift_m": 0.01,
    "twist_rad": 0.6,
    "lift_rad": 0.6,
}

FOLLOW_TIMEOUT_S = 20.0
WRIST_ALIGN_TIMEOUT_S = 60.0
WRIST_MAX_JUMP_PX = 80.0
WRIST_SEG_MIN_SCORE = 25.0
WRIST_CAM_ABOVE_EE = 0.07
# Gate rejections before the memory is treated as stale and re-anchored. At 2,
# the first rejection still coasts — that is the case the gate exists for (the
# target missed for one frame while a twin is detected) — and _localize_retry's
# second look re-anchors instead of turning a wrong memory into a run-ending
# "could not centre".
MEM_COAST_LIMIT = 2
WRIST_SEARCH_ARM = [0.1473, -0.0706, -0.4449, 1.3376, -0.0491]


class _BlobTracker:
    """CamShift color-blob tracker seeded from a Gemini box."""

    def __init__(self, hsv, box, px):
        self.model = vision.seg_model(hsv, box)
        self.window = box
        self.guess = px
        self.misses = 0

    @property
    def ok(self):
        return self.model is not None

    def update(self, hsv):
        """Blob center, or None on miss (keeps last window for retry)."""
        pt, window, _score = vision.seg_track(hsv, self.model, self.window, min_score=WRIST_SEG_MIN_SCORE)
        if pt is not None and math.hypot(pt[0] - self.guess[0], pt[1] - self.guess[1]) > WRIST_MAX_JUMP_PX:
            pt = None
        if pt is None:
            self.misses += 1
            return None
        self.misses = 0
        self.window = window
        self.guess = pt
        return pt


class PickAnyObject(_FloorApproach):
    """Pick up an object lying on the floor, described in natural language
    (e.g. prompt='the white sock', 'a red cup'). The robot localizes the
    object metrically with the head camera, drives above it, grasps, and
    verifies the grasp by backing up and checking the floor. The arm is
    returned to rest either way."""

    manipulation: Manipulation
    head: Head
    # `| None` — best effort, every read is guarded: the wrist stage degrades
    # to the blind grasp without wrist frames. (_FloorApproach declares the
    # mobility/head-camera/odom feeds the approach needs.)
    wrist_image: WristImage | None
    joint_states: JointStates | None

    _p = PARAMS
    _grip_strength: float | None = None
    _holding = False  # fingers committed on an object this run
    # (odom x, odom y, range from the base at that sighting)
    _last_seen: tuple[float, float, float] | None = None
    _coasts = 0  # consecutive looks the memory gate rejected

    def _base_to_odom(self, xy):
        """base_link floor point -> odom frame, or None without odometry."""
        o = self._odom_xyt()
        if o is None:
            return None
        ox, oy, th = o
        c, s = math.cos(th), math.sin(th)
        return (ox + c * xy[0] - s * xy[1], oy + s * xy[0] + c * xy[1])

    def _sighting(self, cand):
        """Candidate pixel -> (odom x, odom y, range from the base), or None
        when it doesn't back-project or odometry is missing."""
        xy = pixel_to_floor(cand[0], cand[1], self._p["tilt_deg"])
        wxy = self._base_to_odom(xy) if xy else None
        return (wxy[0], wxy[1], math.hypot(xy[0], xy[1])) if wxy else None

    def _memory_dist(self, cand):
        """Metres from a candidate to the remembered target; inf when the
        back-projection or odometry is unavailable."""
        seen = self._sighting(cand)
        if seen is None or self._last_seen is None:
            return math.inf
        return math.hypot(seen[0] - self._last_seen[0], seen[1] - self._last_seen[1])

    def _choose_cand(self, cands):
        """Object permanence: among several identical-looking matches, keep the
        instance we've been tracking — the one nearest to where the target was
        last seen (odom frame, so the memory survives base motion between
        looks). A best match outside the gate is a different instance, so the
        target counts as not seen this frame rather than re-anchoring on a
        twin. Gemini's best-first order only breaks the first look.

        The pick of the nearest candidate is immune to back-projection bias
        (identical twins in one frame share it); only the absolute gate is
        not, hence its range term."""
        if self._last_seen is None:
            return cands[0]
        best = min(cands, key=self._memory_dist)
        d = self._memory_dist(best)
        gate = self._p["mem_gate_m"] + self._p["mem_gate_frac"] * self._last_seen[2]
        if d > gate:
            self._coasts += 1
            if self._coasts < MEM_COAST_LIMIT:
                self.logger.info(
                    f"[PickAnyObject] {len(cands)} match(es), nearest {d:.2f}m from last sighting "
                    f"(gate {gate:.2f}m) — coasting"
                )
                return None
            # Disagreeing look after look means the memory is stale, not that
            # the object teleported. Drop it and re-anchor: coasting forever
            # would strand the run in "could not centre" while Gemini is
            # returning the object in every frame.
            self.logger.info(f"[PickAnyObject] memory {d:.2f}m off for {self._coasts} looks — re-anchoring")
            self._last_seen = None
            self._coasts = 0
            return cands[0]
        self._coasts = 0
        if len(cands) > 1:
            self.logger.info(
                f"[PickAnyObject] {len(cands)} matches; kept #{cands.index(best) + 1} ({d:.2f}m from last sighting)"
            )
        return best

    def _detect_px(self, prompt):
        """Head frame -> grasp pixel of the remembered target, or None. Also
        records Gemini's per-object grip_strength for the close."""
        text, img = self._ask_head(
            f"Find '{prompt}' lying on the floor in this image. Match precisely — "
            "not paper/packaging when asked for clothing, and NOT anything held "
            "by the robot arm. Return ONLY a JSON list of ALL matches (every "
            "one, if several look alike), each "
            '{"box_2d":[ymin,xmin,ymax,xmax], "grasp_point":[y,x], '
            '"grip_strength":s} normalized 0-1000, best first. grasp_point is '
            "the CENTER of the object (geometric middle of the visible blob), "
            "not an edge or tip. grip_strength is how hard a parallel gripper "
            "should squeeze this object, 0.30-0.60: soft/deformable objects "
            "(socks, fabric, plush) need 0.60 or they slip out; rigid/hard "
            "objects (metal, hard plastic, wood, ceramic) need 0.30-0.40 — "
            "squeezing them harder stalls the gripper servo. "
            "Empty list if not present."
        )
        cands = vision.parse_det_cands(text)
        cand = self._choose_cand(cands) if cands else None
        if cand is None:
            return None
        u, v, grip = cand
        if grip is not None:
            lo, hi = GRIP_STRENGTH_RANGE
            self._grip_strength = max(lo, min(hi, grip))
        seen = self._sighting(cand)
        if seen is not None:
            self._last_seen = seen
        self._debug("detect", image=img, label=prompt, box_px=vision.parse_det_box(text), point_px=[u, v])
        return (u, v)

    def _rest_arm(self, keep_grip):
        """Best-effort teardown: carry if holding, else fold to rest. Never
        raises. REST, not ZERO: after a failed descent the arm can be near the
        floor, and the zero posture would sweep the gripper through it."""
        joints = CARRY_ARM + [-self._p["close_strength"]] if keep_grip else list(self.manipulation.REST)
        try:
            self.manipulation.move_joints(joints, duration=3.0)
            # Committed teardown (runs after cancel): time.sleep on purpose,
            # then re-command so the servos settle under the shifted load.
            time.sleep(0.3)
            self.manipulation.move_joints(joints, duration=3.0)
        except Exception as e:  # noqa: BLE001 — teardown must not mask the run result
            self.logger.warning(f"[PickAnyObject] rest-arm failed: {e}")

    def _wrist_seed(self, prompt):
        """Wrist Gemini box -> (center_px, box) or (None, None)."""
        self.sleep(self._p["wrist_settle_s"])
        img = self.wrist_image
        text = (
            gemlib.ask_image(
                self._proxy,
                img,
                f"Wrist camera on a robot gripper, looking down at the floor. "
                f"Find '{prompt}' on the floor. Ignore the gripper fingers "
                "themselves. Return ONLY a JSON list of ALL matches, each "
                '{"box_2d":[ymin,xmin,ymax,xmax]} normalized 0-1000, best first, '
                "each box TIGHT around its object. Empty list if not visible.",
                logger=self.logger,
            )
            if img
            else None
        )
        boxes = vision.parse_det_boxes(text)
        # The base already centred the tracked instance under the arm, so among
        # identical twins the box nearest the servo aim point is ours.
        box = min(boxes, key=self._wrist_aim_dist) if boxes else None
        px = (box[0] + box[2] / 2.0, box[1] + box[3] / 2.0) if box else None
        return px, box

    def _wrist_aim_dist(self, box):
        u, v = box[0] + box[2] / 2.0, box[1] + box[3] / 2.0
        return math.hypot(u - self._p["wrist_box_u"], v - self._p["wrist_box_v"])

    def _next_wrist_hsv(self, last_b64, timeout=1.5):
        """Wait for a new wrist frame -> (hsv|None, b64)."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            # Identity, not content: sim frames can be byte-identical (see
            # _follow_into_box).
            img = self.wrist_image
            if img and img is not last_b64:
                # Consume the frame even when it won't decode (truncated JPEG
                # under load): re-decoding the same buffer every poll would
                # burn the whole timeout on ~40 decodes of a known-bad frame.
                last_b64 = img
                hsv = vision.b64_to_hsv(img)
                if hsv is not None:
                    return hsv, img
            self.sleep(0.04)
        return None, last_b64

    def _wrist_done(self, x, y, z, reason):
        self.logger.info(f"[PickAnyObject] wrist stage: {reason} (z={z:.3f})")
        return x, y, z

    def _wrist_reseed(self, prompt, raw):
        """Persistent tracking loss: one Gemini look + a fresh color model,
        since the view has changed. -> (tracker|None, raw, fail_reason)."""
        px, box = self._wrist_seed(prompt)
        if px is None:
            return None, raw, "lost track"
        hsv, raw = self._next_wrist_hsv(raw)
        if hsv is None:
            return None, raw, "no wrist frames"
        tracker = _BlobTracker(hsv, box, px)
        if not tracker.ok:
            return None, raw, "lost track"
        return tracker, raw, ""

    def _wrist_descend(self, prompt, tx, ty):
        """Wrist CamShift servo down to wrist_stop_z: nudge toward the wrist
        box, or step down once the object has been seen inside it twice.
        A miss gets 2 frames of patience, then a Gemini re-seed (budget =
        wrist_steps - 1). Color model, not LK: the object grows/deforms
        during the descent and optical flow slides off.
        Returns (x,y,z); falls back to (tx,ty) if never seen."""
        p = self._p
        try:
            ee = self.manipulation.pose.position
        except ArmFailed:
            ee = None
        z = ee[2] if ee else p["hover_z"]
        looks = int(p["wrist_steps"]) - 1

        px, box = self._wrist_seed(prompt)
        if px is None:
            return self._wrist_done(tx, ty, z, "not seen")
        x, y = (ee[0], ee[1]) if ee else (tx, ty)

        hsv, raw = self._next_wrist_hsv(None)
        if hsv is None:
            return self._wrist_done(tx, ty, z, "no wrist frames")
        tracker = _BlobTracker(hsv, box, px)
        if not tracker.ok:
            return self._wrist_done(tx, ty, z, "not seen")

        deadline = time.monotonic() + WRIST_ALIGN_TIMEOUT_S
        streak = 0  # verified matches since the arm last moved
        centered = 0  # consecutive matches INSIDE the box
        stalled = 0  # consecutive steps eaten by the reach clamp
        reason = "reached stop z"
        while z > p["wrist_stop_z"] + 1e-6:
            # Explicit cancel point: with a fresh frame already buffered (the
            # common case after each blocking move) _next_wrist_hsv returns
            # without ever sleeping, so a Stop would ride the whole descent.
            self.check_cancelled()
            if time.monotonic() > deadline:
                reason = "timeout"
                break
            hsv, raw = self._next_wrist_hsv(raw)
            if hsv is None:
                reason = "no wrist frames"
                break

            px = tracker.update(hsv)
            if px is None:
                streak = centered = 0
                if tracker.misses < 3:
                    continue  # transient (blur / mid-move frame) — wait
                if looks <= 0:
                    reason = "lost track"
                    break
                looks -= 1
                tracker, raw, fail = self._wrist_reseed(prompt, raw)
                if tracker is None:
                    reason = fail
                    break
                px = tracker.guess
            streak += 1

            err_u = px[0] - p["wrist_box_u"]
            err_v = px[1] - p["wrist_box_v"]
            inside = inside_box(px, p["wrist_box_u"], p["wrist_box_v"], p["wrist_half_px"])
            centered = centered + 1 if inside else 0
            if streak < 2:
                continue  # watch one more frame before trusting it
            if inside and centered < 2:
                continue  # just entered the box — confirm it stays

            stepped_down = centered >= 2
            if stepped_down:
                z = max(p["wrist_stop_z"], z - p["wrist_z_step"])
                # Descending IS progress: only consecutive clamped nudges count
                # as stalled, or three clamps spread across a long tracking
                # descent would abort a servo that is working.
                stalled = 0
            else:
                # Gains tuned at z=0.15; scale with camera height.
                s = (z + WRIST_CAM_ABOVE_EE) / (0.15 + WRIST_CAM_ABOVE_EE)
                cap = p["wrist_step_max"]
                step_x = max(-cap, min(cap, p["wrist_kx"] / 100.0 * err_v * s))
                step_y = max(-cap, min(cap, p["wrist_ky"] / 100.0 * err_u * s))
                nx, ny = self.manipulation.clamp_reach(x + step_x, y + step_y)
                # The reach clamp can eat the whole step (object at the edge of
                # the reach box) — hand off to the blind push instead of
                # re-commanding the same clamped pose until timeout.
                if math.hypot(nx - x, ny - y) < 0.25 * math.hypot(step_x, step_y):
                    stalled += 1
                    if stalled >= 3:
                        reason = "reach limit"
                        break
                    continue
                stalled = 0
                x, y = nx, ny
                tracker.guess = (p["wrist_box_u"], p["wrist_box_v"])
            self.manipulation.move_to(x, y, z, pitch=p["wrist_pitch"], duration=p["wrist_move_s"])
            if stepped_down:
                # A pure z-hop barely shifts the view: one fresh confirming
                # frame is enough, so hops chain instead of re-earning 2+2.
                streak = 1
            else:
                streak = 0
                centered = 0  # view shifted — re-confirm centering

        return self._wrist_done(x, y, z, reason)

    def _goto_search_pose(self, bearing):
        """WRIST_SEARCH_ARM aimed at bearing; pins IK to elbow-up branch.

        j6 commands GRIPPER_OPEN instead of echoing a live j6 read: right
        after the gripper open the joint_states snapshot is one tick
        stale, and re-commanding it would close the just-opened gripper."""
        a = WRIST_SEARCH_ARM
        pose = [bearing, a[1], a[2], self._p["wrist_pitch"] - a[1] - a[2], a[4], self.manipulation.GRIPPER_OPEN]
        self.manipulation.move_joints(pose, duration=self._p["hover_s"])
        self.sleep(0.3)

    def _push_to_floor(self, x, y, z_from):
        """Blind descent to floor as ONE multi-waypoint trajectory — the
        rung-by-rung version decelerated at every rung and looked choppy.
        Contact just stalls the final segments; abort if still high."""
        p = self._p
        self.check_cancelled()
        rungs = [z for z in (p["descend_z1"], p["descend_z2"], p["descend_z3"], p["floor_z"]) if z < z_from - 1e-6]
        if rungs:
            # grip=GRIPPER_OPEN re-asserts an open claw even if it drifted
            # shut during the wrist descent (never re-seed from measured).
            waypoints = [Waypoint(x, y, z, pitch=p["arm_pitch"], duration=p["descend_s"]) for z in rungs]
            try:
                self.manipulation.follow(waypoints, grip=self.manipulation.GRIPPER_OPEN)
            except ArmFailed as e:
                # Contact-stall or transient rejection is expected down here —
                # recover and let the height check below decide.
                self.logger.warning(f"[PickAnyObject] descent failed ({e}); recovering")
                self.manipulation.recover()
        # Covers the blind path only: after a wrist align the EE already
        # starts below the abort height — a limp arm there is caught by
        # _grasp_verified instead.
        try:
            ee_z = self.manipulation.pose.z
        except ArmFailed:
            ee_z = None
        if ee_z is not None and ee_z > p["descend_abort_z"]:
            self.manipulation.recover()
            raise ArmUnhealthy("arm would not descend")

    def _arm_joints(self):
        """The 6 current joint positions; raises LookupError when joint
        states are missing or short (callers fall back to IK)."""
        js = self.joint_states
        if js is None or len(js.position) < 6:
            raise LookupError("joint states missing or short")
        return list(js.position[:6])

    def _close_twist_lift(self, x, y):
        """Close, joint-space twist+lift (IK would unwind j5). Uses time.sleep
        on purpose: the fingers have committed, and a cancel must not unwind
        mid-grip — the run finishes this and the teardown carries the object.
        Closing on air reaches ~GRIPPER_EMPTY_J6, which _grasp_verified's j6
        check catches."""
        p = self._p
        if p["close_lift_m"] > 0:
            try:
                ee_z = self.manipulation.pose.z
                self.manipulation.move_to(
                    x,
                    y,
                    ee_z + p["close_lift_m"],
                    pitch=p["arm_pitch"],
                    duration=0.5,
                    tolerance_xy=None,
                    tolerance_z=None,
                )
            except ArmFailed:
                pass  # best-effort pre-close lift; the grasp decides below
        try:
            self.manipulation.gripper_close(p["close_strength"], duration=p["close_s"])
        except ArmFailed as e:
            raise ArmUnhealthy(f"gripper would not close: {e}") from e
        # Fingers have committed: from here teardown must fold with the grip
        # kept, not open over the floor mid-carry — only a verified miss
        # clears the flag. Set here, not after _grasp_at returns: an exception
        # in the twist/lift below (e.g. ArmUnhealthy from the LookupError
        # fallback) must not release a just-grasped object on the way home.
        self._holding = True
        time.sleep(p["close_settle_s"])

        grip = -p["close_strength"]
        # The twist winds FABRIC onto the fingers; on a rigid shell it helps
        # eject the object. Gemini's grip_strength doubles as the hardness signal.
        soft = self._grip_strength is None or self._grip_strength >= SOFT_GRIP_MIN
        lifted = False
        try:
            if soft:
                j = self._arm_joints()
                j[4] = max(-1.4, min(1.4, j[4] + p["twist_rad"]))
                j[5] = grip
                self.manipulation.move_joints(j, duration=1.0)
                time.sleep(0.3)
            j = self._arm_joints()
            j[1] = max(-1.4, j[1] - p["lift_rad"])
            j[5] = grip
            self.manipulation.move_joints(j, duration=2.0)
            lifted = True
            time.sleep(0.3)
        except LookupError:
            pass
        except ArmFailed as e:
            # A skipped lift would leave the EE at floor_z for
            # _grasp_verified's 0.15 m backup drive — fall through to the
            # FK-verified cartesian lift below.
            self.logger.warning(f"[PickAnyObject] joint lift failed ({e}); trying cartesian lift")
        if not lifted:
            # The standing grip target (set by gripper_close above) rides
            # along — never the measured j6, which is stalled on the object
            # and would zero the grip preload. move_to verifies by FK and
            # recover-retries, so the arm is confirmed off the floor (or the
            # run fails cleanly and teardown carries with the grip kept).
            self.manipulation.move_to(x, y, 0.22, pitch=p["arm_pitch"], duration=2.0, tolerance_xy=0.10)

    def _grasp_at(self, prompt, xy):
        """Full grasp at floor xy (base_link)."""
        p = self._p
        x, y = self.manipulation.clamp_reach(xy[0] - p["grasp_x_off"], xy[1])

        self.manipulation.torque_on()
        # gripper_open reboots + retries a tripped servo, raising ArmUnhealthy
        # if the claw stays shut.
        self.manipulation.gripper_open(duration=1.0)
        if p["wrist_steps"] >= 1:
            self._goto_search_pose(math.atan2(y, x))
            x, y, z = self._wrist_descend(prompt, x, y)
        else:
            z = p["hover_z"]
            self.manipulation.move_to(x, y, z, pitch=p["arm_pitch"], duration=p["hover_s"])

        self._push_to_floor(x, y, z)
        self.check_cancelled()  # last exit before the fingers commit
        self._close_twist_lift(x, y)

    def _grasp_verified(self, prompt):
        """Back up, then check floor clear + gripper not open. Gemini gets both
        cameras: the wrist view can show the object in the fingers, so a held
        object isn't mistaken for a dropped one."""
        self._drive(-VERIFY_BACKUP_M)
        self.sleep(self._p["settle_s"])
        js = self.joint_states
        j6 = js.position[5] if js is not None and len(js.position) > 5 else None
        main_img, wrist_img = self.main_image, self.wrist_image
        images = [img for img in (main_img, wrist_img) if img]
        labels = []
        if main_img:
            labels.append(f"Image {len(labels) + 1} is the head camera looking at the floor.")
        if wrist_img:
            labels.append(
                f"Image {len(labels) + 1} is the WRIST camera next to the gripper "
                "fingers (mirrored) — the object may be visible held in the fingers there."
            )
        floor_text = (
            gemlib.ask_image(
                self._proxy,
                images,
                f"Robot just tried to pick up '{prompt}' and backed up a step. "
                f"{' '.join(labels)} "
                f"Is '{prompt}' lying loose on the floor/carpet DIRECTLY in "
                "front of the robot (the spot it just grabbed at), OUT of the "
                "robot's gripper? An object held between the gripper fingers "
                "counts as grabbed even if it is still touching or resting on "
                "the floor — answer NO for that, as for anything hanging from "
                "the gripper. If several similar objects are visible, judge "
                "ONLY the grab spot just ahead — identical objects lying "
                "elsewhere do not count. Answer YES only if the object is on "
                "the floor free of the gripper. Answer only YES or NO.",
                logger=self.logger,
            )
            if images
            else None
        )
        j6_ok = j6 is not None and j6 > GRIPPER_EMPTY_J6 + 0.02
        # Token scan, not a prefix match: replies like "The object is not on
        # the floor." answer correctly without leading with the word, and the
        # old anchored match counted them — and an empty reply — as
        # floor-not-clear, reporting a demonstrably held object as a miss and
        # then releasing it in teardown. \b keeps hedges out: "CANNOT" and
        # "NOT SURE" contain no whole-word NO or YES.
        verdict = (floor_text or "").upper()
        said_no = re.search(r"\bNO\b", verdict) is not None
        said_yes = re.search(r"\bYES\b", verdict) is not None
        if said_no == said_yes:
            # Empty, hedged, or contradictory reply — same as no vision
            # verdict at all (no cameras, or the call failed after retries):
            # fall back to the gripper evidence alone rather than report a
            # demonstrably held object as a missed grasp.
            held = j6_ok
        else:
            held = said_no and j6_ok
        self.logger.info(
            f"[PickAnyObject] verify: floor={floor_text!r} j6={j6} "
            f"({len(images)} cams) -> {'HELD' if held else 'NOT HELD'}"
        )
        return held

    def execute(self, prompt: str = "the sock") -> SkillReturn:
        """Pick up `prompt` from the floor."""
        if self._proxy is None:
            self.fail("Innate proxy not configured (INNATE_SERVICE_KEY)")

        # Per-run reset: don't carry the last run's object or grip rating.
        self._grip_strength = None
        self._holding = False
        self._last_seen = None
        self._coasts = 0
        try:
            self.head.set_position(int(round(self._p["tilt_deg"])))
            # Fold clear of the head camera before searching
            # (5-joint move: the claw keeps whatever it currently holds).
            self.manipulation.move_joints(NAV_ARM, duration=3.0)

            self.say(f"Looking for {prompt}.")
            xy = self._search(prompt)
            xy = self._position_above(prompt, xy)
            self.say("Picking it up.")
            self._grasp_at(prompt, xy)
            # _close_twist_lift latched self._holding the moment the fingers
            # committed — only a verified miss clears it.
            if not self._grasp_verified(prompt):
                self._holding = False
                self.say("I couldn't get a grip on it.")
                raise SkillFailed(f"Grasp missed — '{prompt}' is still on the floor (verified after backing up)")
            self.say("Got it.")
            return f"Picked up '{prompt}' (verified: floor clear after backing up)"
        except ArmFailed as e:
            # A clean arm give-up is a skill failure, not a crash. SkillFailed
            # and SkillCancelled propagate untouched — the framework owns them.
            self.fail(str(e))
        except ArmUnhealthy as e:
            self.say("My arm isn't responding properly, stopping.")
            self.fail(f"Arm servo failure: {e}")
        finally:
            self.mobility.stop()
            self._rest_arm(keep_grip=self._holding)
            self.head.set_position(0)
