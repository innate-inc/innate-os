# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Pick an object from the floor by text prompt.

Metric localize (pixel -> floor -> base_link), drive into a pick box,
grasp (optional wrist visual-servo), verify by backing up.
No depth camera — URDF + pinhole model.
"""

import base64
import json
import math
import os
import re
import time

from innate_skills.approach import APPROACH_PARAMS, FloorApproach, ask_head, base_to_odom, inside_box

from innate import (
    Head,
    HeadState,
    JointStates,
    MainImage,
    Manipulation,
    Mobility,
    Odometry,
    Skill,
    SkillReturn,
    Waypoint,
    WristImage,
    resource,
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

# Pick parameters, tuned on hardware; the find/position half is APPROACH_PARAMS.
# Grasping at the 0.40 reach edge stalls the wrist and overloads servo 2.
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
    # mid-frame aims short of them. Keep the hardware-tuned 350 target on a
    # robot; the simulator camera model needs 310 or the fingertip centre ends
    # up about 3 cm beyond a floor target. The simulator launcher exports its
    # world-state port into every service container.
    "wrist_box_v": 310.0 if os.environ.get("INNATE_WORLD_STATE_PORT") else 350.0,
    "wrist_half_px": 60.0,
    # The broad box gets the arm down quickly; near the floor, require the
    # object to be genuinely central before committing to a grasp.
    "wrist_final_half_px": 20.0,
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
    # Two bounded retries after the encoder proves the claw closed on air. The
    # normal attempt remains carpet-safe; only a confirmed miss goes 1 cm
    # lower, before the robot has backed away from the target.
    "retry_floor_z": 0.02,
    "grasp_retries": 2.0,
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
WRIST_CAMERA_RECOVERY_S = 5.0
WRIST_CAMERA_GAP_S = 0.75
WRIST_CAMERA_RECOVERIES = 2
WRIST_MAX_JUMP_PX = 80.0
WRIST_SEG_MIN_SCORE = 25.0
WRIST_CAM_ABOVE_EE = 0.07
# Wrist roll to the blob's minor axis (the gripper's 81 mm jaw is narrower
# than most objects' long side). Blobs rounder than MIN_ELONGATION have no
# axis worth chasing; below AXIS_MIN_Z the fingers straddle the blob in the
# wrist view and clip its ends, so the last trusted reading is kept.
MIN_ELONGATION = 1.3
ROLL_MAX = 1.5
AXIS_MIN_Z = 0.07
# Rolls under ROLL_MIN are not worth leaving the hardware-tuned unrolled
# grasp for. ROLL_SIGN is verified in sim only: a mirrored wrist camera (as
# the Gemini prompts below describe the real one) needs -1.
ROLL_MIN = 0.1
ROLL_SIGN = 1.0
# Rolled fingers are level only with the tool axis vertical: at arm_pitch
# 1.30 a 90 deg roll drops one fingertip 3 cm below the other, which lands on
# the object and stalls the descent with the other pad above it.
ROLLED_PITCH = math.pi / 2
# Gate rejections before the memory is treated as stale and re-anchored. At 2,
# the first rejection still coasts — that is the case the gate exists for (the
# target missed for one frame while a twin is detected) — and _localize_retry's
# second look re-anchors instead of turning a wrong memory into a run-ending
# "could not centre".
MEM_COAST_LIMIT = 2
WRIST_SEARCH_ARM = [0.1473, -0.0706, -0.4449, 1.3376, -0.0491]
# Same camera pitch and reach, without raising to 20cm only to descend again.
# URDF FK: EE near (0.315,-0.053,0.10). Used only when every joint travels no
# farther than in the original search move, at the same duration.
LOW_WRIST_SEARCH_ARM = [0.1473, -0.08003764, 0.30662779, 0.59340985, -0.0491]
# Only for model-confirmed thin, flat rigid targets with clearance at 7cm.
FLAT_WRIST_SEARCH_ARM = [0.1473, -0.07563421, 0.53055148, 0.36508273, -0.0491]


class _BlobTracker:
    """CamShift color-blob tracker seeded from a Gemini box."""

    def __init__(self, hsv, box, px):
        self.model = vision.seg_model(hsv, box)
        self.window = box
        self.guess = px
        self.misses = 0
        self.axis: vision.Axis | None = None

    @property
    def ok(self):
        return self.model is not None

    def update(self, hsv):
        """Blob center, or None on miss (keeps last window for retry)."""
        pt, window, _score, axis = vision.seg_track(hsv, self.model, self.window, min_score=WRIST_SEG_MIN_SCORE)
        if pt is not None and math.hypot(pt[0] - self.guess[0], pt[1] - self.guess[1]) > WRIST_MAX_JUMP_PX:
            pt = None
        if pt is None:
            self.misses += 1
            return None
        self.misses = 0
        self.window = window
        self.guess = pt
        self.axis = axis
        return pt


class PickAnyObject(Skill):
    """Pick up an object lying on the floor, described in natural language
    (e.g. prompt='the white sock', 'a red cup'). The robot localizes the
    object metrically with the head camera, drives above it, grasps, and
    verifies the grasp by backing up and checking the floor. The arm is
    returned to a carry posture when holding, or rest after a miss."""

    manipulation: Manipulation
    mobility: Mobility
    head: Head
    head_position: HeadState | None
    # `| None` — best effort: the approach falls back to stepwise re-detection
    # without head frames, the wrist stage to the blind grasp without wrist ones.
    main_image: MainImage | None
    wrist_image: WristImage | None
    joint_states: JointStates | None
    odom: Odometry | None

    _p = PARAMS

    @resource
    def _proxy(self):
        return gemlib.make_client()

    _grip_strength: float | None = None
    _holding = False  # fingers committed on an object this run
    # (odom x, odom y, range from the base at that sighting)
    _last_seen: tuple[float, float, float] | None = None
    _coasts = 0  # consecutive looks the memory gate rejected

    def _sighting(self, cand):
        """Candidate pixel -> (odom x, odom y, range from the base), or None
        when it doesn't back-project or odometry is missing."""
        xy = pixel_to_floor(cand[0], cand[1], self._p["tilt_deg"])
        wxy = base_to_odom(self.mobility.odom_xyt(self.odom), xy) if xy else None
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
        """Head frame -> remembered target pixel and material handling style."""
        if self._pickup_policy is not None:
            self.mobility.stop()
            head_image = self._settled_head_image()
            reference = None
            if getattr(self, "_metric_pickup", False):
                reference = self.wait_for(lambda: getattr(self, "rgbd", None), timeout=2)
                if reference is None:
                    raise SkillFailed("No fresh calibrated head depth for metric pickup")
                head_image = base64.b64encode(reference.jpeg).decode("ascii")
            observed = self._observe_pickup(prompt, head_image, "head_metric" if reference else "head")
            text = json.dumps(observed["detections"])
            # Perception overlaps the fold; finish it before any base search.
            # On cancellation/failure, execute's finally joins it before teardown.
            self._finish_nav_fold()
        else:
            text, img = ask_head(
                self,
                self._proxy,
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
                "Empty list if not present.",
                self._p["settle_s"],
            )
        cands = vision.parse_det_cands(text)
        cand = self._choose_cand(cands) if cands else None
        if cand is None:
            return None
        if self._pickup_policy is not None:
            plan = observed["detections"][cands.index(cand)]
            self._search_clearance = plan["search_clearance"]
            self._head_reference = {"image": head_image, "box_2d": plan["box_2d"]}
            if reference is not None:
                self._metric_reference = (reference, plan)
        u, v, grip = cand
        if grip is not None:
            lo, hi = GRIP_STRENGTH_RANGE
            self._grip_strength = max(lo, min(hi, grip))
        seen = self._sighting(cand)
        if seen is not None:
            self._last_seen = seen
        return (u, v)

    def _settled_head_image(self):
        """Use measured head/base settling and a subsequent frame when available.

        Missing, stale or moving telemetry keeps the original settling delay.
        Two distinct head and odometry samples must confirm a stationary view;
        merely receiving the commanded angle is not evidence of settling.
        """
        deadline = time.monotonic() + self._p["settle_s"]
        last_head = last_odom = None
        settled_since = None
        settled_frame = None
        while time.monotonic() < deadline:
            self.check_cancelled()
            head, odom = self.head_position, self.odom
            if head is not None and odom is not None:
                fresh = (
                    head.raw_source is not None
                    and head.raw_source is not last_head
                    and odom.raw_source is not None
                    and odom.raw_source is not last_odom
                )
                stationary = (
                    abs(head.pitch_degrees - self._p["tilt_deg"]) <= 1.0
                    and abs(odom.linear_velocity) <= 0.005
                    and abs(odom.angular_velocity) <= 0.01
                )
                if not stationary:
                    settled_since = settled_frame = None
                elif fresh:
                    last_head, last_odom = head.raw_source, odom.raw_source
                    if settled_since is None:
                        settled_since = time.monotonic()
                    elif time.monotonic() - settled_since >= 0.15 and settled_frame is None:
                        settled_frame = self.main_image
                    elif settled_frame is not None:
                        frame = self.main_image
                        if frame and frame is not settled_frame:
                            return frame
            self.sleep(0.04)
        return self.main_image

    def _finish_nav_fold(self):
        if getattr(self, "_nav_pending", False):
            try:
                self.manipulation.wait()
            finally:
                self._nav_pending = False

    def _observe_pickup(self, prompt, image, view):
        if not image:
            raise SkillFailed(f"No {view} image for pickup")
        try:
            return self._pickup_policy.locate(
                prompt,
                image,
                self.sleep,
                self.check_cancelled,
                view=view,
                reference=getattr(self, "_head_reference", None) if view in {"wrist", "wrist_verify"} else None,
            )
        except ValueError as error:
            raise SkillFailed(str(error)) from None

    def _rest_arm(self, keep_grip):
        """Best-effort teardown: carry if holding, else fold to rest. Never
        raises. REST, not ZERO: after a failed descent the arm can be near the
        floor, and the zero posture would sweep the gripper through it."""
        if (
            keep_grip
            and getattr(self, "_pickup_policy", None) is not None
            and self._grip_strength is not None
            and self._grip_strength < SOFT_GRIP_MIN
        ):
            # A verified raised rigid grasp is already a carry position. Folding
            # it to a fixed wrist roll can eject the object. Leave the standing
            # joint/grip targets intact; never reseed grip from measured aperture.
            try:
                j6 = self._arm_joints()[5]
                z = self.manipulation.pose.z
                if math.isfinite(z) and z >= self._p["floor_z"] + 0.07 and j6 > GRIPPER_EMPTY_J6 + 0.02:
                    self.logger.info("[PickAnyObject] keeping the raised rigid grasp for carry")
                    return
            except (ArmFailed, ArmUnhealthy, LookupError):
                pass
        joints = CARRY_ARM + [-self._p["close_strength"]] if keep_grip else list(self.manipulation.REST)
        try:
            self.manipulation.move_joints(joints, duration=3.0)
            # Committed teardown (runs after cancel): time.sleep on purpose,
            # then re-command so the servos settle under the shifted load.
            time.sleep(0.3)
            self.manipulation.move_joints(joints, duration=3.0)
        except Exception as e:  # noqa: BLE001 — teardown must not mask the run result
            self.logger.warning(f"[PickAnyObject] rest-arm failed: {e}")

    def _wrist_seed(self, prompt, frame=None):
        """Wrist detection -> tracking box and, for Astra, a grasp plan."""
        if self._pickup_policy is not None:
            # The search already finished and settled. Wait for an actual new
            # frame instead of another fixed pause, and refuse a frozen camera.
            hsv, img = (
                frame if frame is not None else self._next_wrist_hsv(self.wrist_image, timeout=WRIST_CAMERA_RECOVERY_S)
            )
            if hsv is None:
                raise SkillFailed("No fresh wrist image for pickup")
            detections = self._observe_pickup(prompt, img, "wrist")["detections"]
            choices = [(box, plan) for plan in detections for box in vision.parse_det_boxes(json.dumps([plan]))]
            if not choices:
                raise SkillFailed("Pickup target not confidently visible in wrist camera")
            box, plan = min(choices, key=lambda choice: self._wrist_aim_dist(choice[0]))
            axis = plan["axis_2d"]
            self._planned_roll = 0.0
            if axis:
                # Normalized coordinates have different horizontal/vertical
                # scales. Convert to image pixels before computing the axis.
                dy = (axis[2] - axis[0]) * vision.IMG_H
                dx = (axis[3] - axis[1]) * vision.IMG_W
                theta = math.atan2(dy, dx) % math.pi
                roll = ROLL_SIGN * (theta - math.pi / 2)
                self._planned_roll = max(-ROLL_MAX, min(ROLL_MAX, roll))
            self._wrist_seed_frame = (hsv, img)
            point = plan["grasp_point_2d"]
            return (point[1] * vision.IMG_W / 1000, point[0] * vision.IMG_H / 1000), box
        else:
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

    def _wrist_done(
        self, x: float, y: float, z: float, reason: str, axis: vision.Axis | None = None
    ) -> tuple[float, float, float, float]:
        if self._pickup_policy is not None:
            if reason != "reached stop z" or self._planned_roll is None:
                raise SkillFailed(f"Wrist pickup alignment failed: {reason}")
            roll = self._planned_roll
        else:
            roll = self._grasp_roll(axis)
        self.logger.info(
            f"[PickAnyObject] wrist stage: {reason} "
            f"(x={x:.3f}, y={y:.3f}, z={z:.3f}, roll={math.degrees(roll):+.0f} deg)"
        )
        return x, y, z, roll

    @staticmethod
    def _grasp_roll(axis: vision.Axis | None) -> float:
        """Wrist roll that turns the fingers onto the blob's minor axis, or
        0.0 for the tuned unrolled grasp. The wrist camera rolls with the
        fingers, which close along image u, so the blob's image angle is
        already relative to the jaw: roll = angle from v."""
        if axis is None or axis[1] < MIN_ELONGATION:
            return 0.0
        roll = ROLL_SIGN * (axis[0] - math.pi / 2)
        if abs(roll) < ROLL_MIN:
            return 0.0
        return max(-ROLL_MAX, min(ROLL_MAX, roll))

    def _new_wrist_tracker(self, box, px, raw):
        if self._pickup_policy is not None:
            from innate_skills.grasp_tracker import make_grasp_tracker

            # Seed on precisely the frame that supplied the model coordinates.
            hsv, raw = self._wrist_seed_frame
            rigid = GRIP_STRENGTH_RANGE[0] <= self._grip_strength < SOFT_GRIP_MIN
            return make_grasp_tracker(hsv, box, px, rigid=rigid), raw
        hsv, raw = self._next_wrist_hsv(raw)
        return (_BlobTracker(hsv, box, px) if hsv is not None else None), raw

    def _wrist_reseed(self, prompt, raw, frame=None):
        """Reacquire within the existing bounded model budget, while stationary."""
        px, box = self._wrist_seed(prompt) if frame is None else self._wrist_seed(prompt, frame=frame)
        if px is None:
            return None, raw, "lost track"
        tracker, raw = self._new_wrist_tracker(box, px, raw)
        if tracker is None or not tracker.ok:
            return None, raw, "insufficient tracking support"
        return tracker, raw, ""

    def _wrist_descend(self, prompt, tx, ty):
        """Servo a persistent grasp point; confirm fresh frames before motion.

        Astra uses local feature geometry. Classic retains its color tracker.
        Tracking loss stops motion and permits one bounded model reacquisition.
        """
        p = self._p
        try:
            ee = self.manipulation.pose.position
        except ArmFailed:
            ee = None
        z = ee[2] if ee else p["hover_z"]
        looks = int(p["wrist_steps"]) - 1
        recoveries = WRIST_CAMERA_RECOVERIES
        camera_lost = False

        px, box = self._wrist_seed(prompt)
        if px is None:
            return self._wrist_done(tx, ty, z, "not seen")
        x, y = (ee[0], ee[1]) if ee else (tx, ty)

        tracker, raw = self._new_wrist_tracker(box, px, None)
        if tracker is None or not tracker.ok:
            return self._wrist_done(tx, ty, z, "insufficient tracking support")

        last_frame_at = time.monotonic()
        deadline = last_frame_at + WRIST_ALIGN_TIMEOUT_S
        axis = None  # last blob axis read high enough to trust
        streak = 0  # verified matches since the arm last moved
        centered = 0  # consecutive matches INSIDE the box
        stalled = 0  # consecutive steps eaten by the reach clamp
        reason = "reached stop z"
        while z > p["wrist_stop_z"] + 1e-6 or self._pickup_policy is not None:
            # Explicit cancel point: with a fresh frame already buffered (the
            # common case after each blocking move) _next_wrist_hsv returns
            # without ever sleeping, so a Stop would ride the whole descent.
            self.check_cancelled()
            if time.monotonic() > deadline:
                reason = "timeout"
                break
            hsv, raw = self._next_wrist_hsv(raw)
            now = time.monotonic()
            camera_lost = camera_lost or hsv is None or now - last_frame_at >= WRIST_CAMERA_GAP_S
            last_frame_at = now
            if camera_lost and self._pickup_policy is not None:
                # A camera restart can hide the entire last arm movement. Never
                # extend optical flow across that gap or preserve centered votes.
                streak = centered = 0
                if recoveries <= 0:
                    reason = "camera recovery budget exhausted"
                    break
                recoveries -= 1
                self.logger.info("[PickAnyObject] wrist camera interrupted; holding position and reacquiring")
                if hsv is None:
                    remaining = min(WRIST_CAMERA_RECOVERY_S, deadline - time.monotonic())
                    if remaining <= 0:
                        reason = "timeout"
                        break
                    hsv, raw = self._next_wrist_hsv(raw, timeout=remaining)
                if hsv is None:
                    reason = "wrist camera did not recover"
                    break
                self.check_cancelled()
                if time.monotonic() >= deadline:
                    reason = "timeout"
                    break
                tracker, raw, fail = self._wrist_reseed(prompt, raw, frame=(hsv, raw))
                if tracker is None:
                    reason = fail
                    break
                camera_lost = False
                last_frame_at = time.monotonic()
                continue  # two new tracked frames must authorize the next move
            if hsv is None:
                reason = "no wrist frames"
                break

            px = tracker.update(hsv)
            if px is not None and z >= AXIS_MIN_Z and tracker.axis is not None:
                axis = tracker.axis
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
                if self._pickup_policy is not None:
                    # Inference ran while stationary; resume the camera wait
                    # budget now. Keep the seed image and require new frames.
                    last_frame_at = time.monotonic()
                    continue  # require fresh tracked confirmations after reacquisition
                px = tracker.guess
            streak += 1

            err_u = px[0] - p["wrist_box_u"]
            err_v = px[1] - p["wrist_box_v"]
            half_px = p["wrist_final_half_px"] if z <= AXIS_MIN_Z else p["wrist_half_px"]
            inside = inside_box(px, p["wrist_box_u"], p["wrist_box_v"], half_px)
            centered = centered + 1 if inside else 0
            if streak < 2:
                continue  # watch one more frame before trusting it
            if inside and centered < 2:
                continue  # just entered the box — confirm it stays

            if z <= p["wrist_stop_z"] + 1e-6 and centered >= 2:
                break  # fresh confirmation at the final height before closing
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
                if self._pickup_policy is None:
                    tracker.guess = (p["wrist_box_u"], p["wrist_box_v"])
            if self._pickup_policy is not None:
                with tracker.during_motion(lambda: self.wrist_image, vision.b64_to_hsv, raw) as motion:
                    self.manipulation.move_to(x, y, z, pitch=p["wrist_pitch"], duration=p["wrist_move_s"])
                raw = motion["raw"]
                camera_lost = motion.get("gap", False)
                last_frame_at = motion.get("last_frame_at", time.monotonic())
                if not tracker.ok:
                    reason = "tracking worker failed"
                    break
            else:
                self.manipulation.move_to(x, y, z, pitch=p["wrist_pitch"], duration=p["wrist_move_s"])
            if stepped_down:
                # A pure z-hop barely shifts the view: one fresh confirming
                # frame is enough, so hops chain instead of re-earning 2+2.
                streak = 1
            else:
                streak = 0
                centered = 0  # view shifted — re-confirm centering

        return self._wrist_done(x, y, z, reason, axis)

    def _goto_search_pose(self, bearing):
        """Joint-space camera pose aimed at bearing; fixes the IK starting branch.

        j6 commands GRIPPER_OPEN instead of echoing a live j6 read: right
        after the gripper open the joint_states snapshot is one tick
        stale, and re-commanding it would close the just-opened gripper."""
        a = WRIST_SEARCH_ARM
        pose = [bearing, a[1], a[2], self._p["wrist_pitch"] - a[1] - a[2], a[4], self.manipulation.GRIPPER_OPEN]
        if self._pickup_policy is not None:
            search = a
            reach = 0.30
            if self._grip_strength is not None and self._grip_strength < SOFT_GRIP_MIN:
                if self._search_clearance == "flat":
                    search = FLAT_WRIST_SEARCH_ARM
                    reach = 0.306
                elif self._search_clearance == "low":
                    search = LOW_WRIST_SEARCH_ARM
                    reach = 0.315
            # Aim from joint1's URDF origin, not base_link. The fixed search
            # reach follows the selected pose; camera servoing handles the rest.
            arm_bearing = math.atan2(reach * math.sin(bearing) + 0.05285, reach * math.cos(bearing) - 0.086)
            candidate = [
                arm_bearing,
                search[1],
                search[2],
                self._p["wrist_pitch"] - search[1] - search[2],
                search[4],
                pose[5],
            ]
            try:
                current = self._arm_joints()
                if all(
                    abs(new - start) <= abs(old - start) + 1e-6
                    for new, old, start in zip(candidate, pose, current, strict=True)
                ):
                    pose = candidate
            except LookupError:
                pass  # original search is the conservative fallback
        self.manipulation.move_joints(pose, duration=self._p["hover_s"])
        self.sleep(0.3)

    def _prepare_wrist_search(self, bearing):
        if self._pickup_policy is None:
            self.manipulation.gripper_open(duration=1.0)
            self._goto_search_pose(bearing)
            return
        # The search move already commands an open claw over two seconds,
        # slower than the separate one-second open. Keep its verification:
        # any missing, non-finite or not-fully-open reading falls back to the
        # SDK's verified open, including its reboot/retry for a tripped servo.
        try:
            self._goto_search_pose(bearing)
        except ArmFailed:
            # A tripped claw can reject the combined move before its aperture
            # is readable. Preserve the original verified-open recovery first.
            self.manipulation.gripper_open(duration=1.0)
            self._goto_search_pose(bearing)
        try:
            j6 = self._arm_joints()[5]
            opened = math.isfinite(j6) and j6 >= self.manipulation.GRIPPER_OPEN - 0.05
        except LookupError:
            opened = False
        if not opened:
            self.manipulation.gripper_open(duration=1.0)

    def _grasp_orientation(self, x: float, y: float, roll: float) -> tuple[float, float, float]:
        """(roll, pitch, yaw) for the descent and close. Unrolled is the
        hardware-tuned grasp. Rolled needs the tool vertical, and then the
        yaw must be the arm's own bearing: RPY is gimbal-locked at pitch
        pi/2, and a yaw-0 target makes the solver dump the whole base
        rotation into j5 (j5 = roll + j1). Out of the vertical pitch's
        reach, the tuned grasp is kept rather than a descent that never
        starts (follow's IK failure reads as a contact stall)."""
        p = self._p
        if roll == 0.0:
            return 0.0, p["arm_pitch"], 0.0
        yaw = math.atan2(y, x)
        if not self.manipulation.reachable(x, y, p["floor_z"], roll=roll, pitch=ROLLED_PITCH, yaw=yaw):
            self.logger.warning(f"[PickAnyObject] rolled grasp unreachable at ({x:.2f}, {y:.2f}); grasping unrolled")
            return 0.0, p["arm_pitch"], 0.0
        return roll, ROLLED_PITCH, yaw

    def _push_to_floor(
        self,
        x: float,
        y: float,
        z_from: float,
        roll: float,
        pitch: float,
        yaw: float,
        *,
        floor_z: float | None = None,
    ) -> None:
        """Blind descent to floor as ONE multi-waypoint trajectory — the
        rung-by-rung version decelerated at every rung and looked choppy.
        Contact just stalls the final segments; abort if still high."""
        p = self._p
        target_z = p["floor_z"] if floor_z is None else floor_z
        self.check_cancelled()
        rungs = [z for z in (p["descend_z1"], p["descend_z2"], p["descend_z3"], target_z) if z < z_from - 1e-6]
        if rungs:
            # grip=GRIPPER_OPEN re-asserts an open claw even if it drifted
            # shut during the wrist descent (never re-seed from measured).
            waypoints = [Waypoint(x, y, z, roll=roll, pitch=pitch, yaw=yaw, duration=p["descend_s"]) for z in rungs]
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
        if ee_z is not None:
            self.logger.info(f"[PickAnyObject] descent target={target_z:.3f}, settled z={ee_z:.3f}")
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

    def _gripper_closed_on_air(self) -> bool:
        """Whether fresh encoder state proves the last close caught nothing.

        Missing state is deliberately inconclusive: never reopen a possibly
        held object merely because telemetry dropped out.
        """
        js = self.joint_states
        j6 = js.position[5] if js is not None and len(js.position) > 5 else None
        empty = j6 is not None and j6 <= GRIPPER_EMPTY_J6 + 0.02
        self.logger.info(f"[PickAnyObject] grip check: j6={j6} -> {'EMPTY' if empty else 'HELD/UNKNOWN'}")
        return empty

    def _pre_close_lift(self, x: float, y: float, roll: float, pitch: float, yaw: float) -> None:
        p = self._p
        if self._grip_strength is not None and self._grip_strength < SOFT_GRIP_MIN:
            # Shared rigid-grasp correction: a 1cm unpress lifts the pads off
            # thin objects before closing. Keep the existing floor/force limits
            # and close there; soft/unknown material keeps its previous handling.
            return
        if p["close_lift_m"] <= 0:
            return
        try:
            ee_z = self.manipulation.pose.z
            self.manipulation.move_to(
                x,
                y,
                ee_z + p["close_lift_m"],
                roll=roll,
                pitch=pitch,
                yaw=yaw,
                duration=0.5,
                tolerance_xy=None,
                tolerance_z=None,
            )
        except ArmFailed:
            pass  # best-effort pre-close lift; the grasp decides below

    def _close_once(self) -> None:
        p = self._p
        try:
            self.manipulation.gripper_close(p["close_strength"], duration=p["close_s"])
        except ArmFailed as e:
            raise ArmUnhealthy(f"gripper would not close: {e}") from e
        time.sleep(p["close_settle_s"])

    def _prepare_grasp_retry(self, prompt: str, x: float, y: float) -> tuple[float, float, float, float, float]:
        """Reopen, reacquire the object with the wrist, and descend lower."""
        p = self._p
        retry_z = p["retry_floor_z"]
        self._holding = False
        self._prepare_wrist_search(math.atan2(y, x))
        x, y, z, roll = self._wrist_descend(prompt, x, y)
        roll, pitch, yaw = self._grasp_orientation(x, y, roll)
        self._push_to_floor(x, y, z, roll, pitch, yaw, floor_z=retry_z)
        self._pre_close_lift(x, y, roll, pitch, yaw)
        return x, y, roll, pitch, yaw

    def _lift_grasp(self, x: float, y: float, roll: float, yaw: float) -> None:
        """Lift a closed grasp; joint space keeps a rolled wrist wound."""
        p = self._p
        grip = -p["close_strength"]
        # The twist winds FABRIC onto the fingers; on a rigid shell it helps
        # eject the object. Gemini's grip_strength doubles as the hardness signal.
        soft = self._grip_strength is None or self._grip_strength >= SOFT_GRIP_MIN
        lifted = False
        try:
            if soft:
                j = self._arm_joints()
                # A rolled wrist can already sit near the +1.4 stop: twist the way that has room.
                twist = p["twist_rad"] if j[4] + p["twist_rad"] <= 1.4 else -p["twist_rad"]
                j[4] = max(-1.4, min(1.4, j[4] + twist))
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
            # arm_pitch, not the grasp pitch: the vertical tool axis is out of
            # reach at 0.22 m and stops mattering once the object is held.
            self.manipulation.move_to(
                x, y, 0.22, roll=roll, pitch=p["arm_pitch"], yaw=yaw, duration=2.0, tolerance_xy=0.10
            )

    def _close_twist_lift(self, prompt: str, x: float, y: float, roll: float, pitch: float, yaw: float) -> None:
        """Close and lift, reacquiring and retrying a proven miss.

        The encoder is checked both after closing and after the first lift. A
        floor or edge contact can initially hold the claw open even though no
        object comes up; that miss must also retry before the base backs away.
        """
        self._pre_close_lift(x, y, roll, pitch, yaw)
        retries = int(self._p["grasp_retries"])
        for attempt in range(retries + 1):
            self._close_once()
            empty = self._gripper_closed_on_air()
            lifted = False
            # Fingers may initially be held apart by a floor/edge contact. A
            # lift distinguishes that from a grasp before the base moves.
            if not empty:
                self._holding = True
                self._lift_grasp(x, y, roll, yaw)
                lifted = True
                empty = self._gripper_closed_on_air()
                if not empty:
                    return
            if attempt >= retries:
                # Keep the old safe teardown posture even when the final
                # attempt is empty; verification will report the miss.
                self._holding = True
                if not lifted:
                    self._lift_grasp(x, y, roll, yaw)
                return
            self.logger.warning(
                f"[PickAnyObject] grasp attempt {attempt + 1} closed on air; "
                f"re-centering for retry {attempt + 2}/{retries + 1}"
            )
            x, y, roll, pitch, yaw = self._prepare_grasp_retry(prompt, x, y)

    def _grasp_at(self, prompt, xy):
        """Full grasp at floor xy (base_link)."""
        self.check_cancelled()  # the flow-to-wrist handoff must honor a pending Stop
        if getattr(self, "_metric_pickup", False):
            return self._grasp_rgbd(prompt)
        p = self._p
        x, y = self.manipulation.clamp_reach(xy[0] - p["grasp_x_off"], xy[1])

        self.manipulation.torque_on()
        if p["wrist_steps"] >= 1:
            self._prepare_wrist_search(math.atan2(y, x))
            x, y, z, roll = self._wrist_descend(prompt, x, y)
        else:
            self.manipulation.gripper_open(duration=1.0)
            z, roll = p["hover_z"], 0.0
            self.manipulation.move_to(x, y, z, pitch=p["arm_pitch"], duration=p["hover_s"])

        roll, pitch, yaw = self._grasp_orientation(x, y, roll)
        self._push_to_floor(x, y, z, roll, pitch, yaw)
        self.check_cancelled()  # last exit before the fingers commit
        self._close_twist_lift(prompt, x, y, roll, pitch, yaw)

    def _grasp_rgbd(self, prompt):
        """Opt-in bounded compact rigid grasp, with a fresh final wrist veto.

        Unsupported observations abort this prototype. They must never silently
        enter the affine tracker or the blind grasp path.
        """
        from innate_skills.pickup_rgbd import compact_upper_surface, revalidate_material_point, same_wrist_patch

        reference, plan = self._metric_reference
        if plan["grip_strength"] != 0.35 or plan["search_clearance"] not in {"flat", "low"}:
            raise SkillFailed("Metric pickup requires a compact rigid low/flat target")
        current = self.rgbd
        center = revalidate_material_point(
            reference, current, plan["box_2d"], plan["grasp_point_2d"], now_ns=time.time_ns()
        )
        if center is None or compact_upper_surface(current, plan["box_2d"], plan["grasp_point_2d"]) is None:
            raise SkillFailed("Head proposal changed or is outside the compact metric envelope")
        x, y, top = center
        self.logger.info(f"[PickAnyObject] fresh metric upper center: {center}")
        self.manipulation.torque_on()
        self._prepare_wrist_search(math.atan2(y, x))
        # Search motion can occlude or disturb the target. Recheck the original
        # upper material against new sensors before the metric arm motion.
        current = self.rgbd
        confirmed = revalidate_material_point(
            reference, current, plan["box_2d"], plan["grasp_point_2d"], now_ns=time.time_ns()
        )
        if confirmed is None or math.dist(center, confirmed) > 0.008:
            raise SkillFailed("Metric target changed during the search move")
        x, y, top = confirmed
        start = self.manipulation.pose
        stop_z = self._p["wrist_stop_z"]
        if (
            not all(math.isfinite(v) for v in start.position)
            or math.hypot(start.x - x, start.y - y) > 0.04
            or not (stop_z <= start.z <= stop_z + 0.05 + 1e-3)
            or start.z - top < 0.025
        ):
            raise SkillFailed("Metric target is outside the existing low search envelope")
        # ee_link lies on the tool centerline (URDF ee_joint x=.091838).
        # Vertical tool orientation removes a pitch-dependent XY offset.
        roll, pitch, yaw = 0.0, math.pi / 2, math.atan2(y + 0.05285, x - 0.086)
        floor = self._p["floor_z"]
        if not all(
            self.manipulation.reachable(x, y, z, roll=roll, pitch=pitch, yaw=yaw) for z in (start.z, stop_z, floor)
        ):
            raise SkillFailed("Vertical metric grasp is unreachable")
        # Use the original hover duration for XY/orientation, then the existing
        # centimetre-per-half-second descent. Never shorten physical settling.
        self.manipulation.move_to(
            x,
            y,
            start.z,
            roll=roll,
            pitch=pitch,
            yaw=yaw,
            duration=self._p["hover_s"],
            grip=self.manipulation.GRIPPER_OPEN,
            tolerance_xy=0.008,
            tolerance_z=0.008,
        )
        z = start.z
        while z > stop_z + 1e-6:
            self.check_cancelled()
            z = max(stop_z, z - self._p["wrist_z_step"])
            self.manipulation.move_to(
                x,
                y,
                z,
                roll=roll,
                pitch=pitch,
                yaw=yaw,
                duration=self._p["wrist_move_s"],
                grip=self.manipulation.GRIPPER_OPEN,
                tolerance_xy=0.008,
                tolerance_z=0.008,
            )
        self._push_to_floor(x, y, z, roll, pitch, yaw)
        pose = self.manipulation.pose
        if (
            not all(math.isfinite(v) for v in (*pose.position, *pose.rpy))
            or math.hypot(pose.x - x, pose.y - y) > 0.008
            or abs(pose.z - floor) > 0.008
        ):
            raise SkillFailed("Metric pre-close pose did not settle")
        before = self.wrist_image
        decoded, frame = self._next_wrist_hsv(before, timeout=0.5)
        if decoded is None or frame is before or frame is None:
            raise SkillFailed("No new wrist frame at metric endpoint")
        verdict = self._observe_pickup(prompt, frame, "wrist_verify")["detections"]
        if len(verdict) != 1 or not verdict[0]["aligned"]:
            raise SkillFailed("Fresh wrist view did not confirm target between open pads")
        # Inference is historical by completion. Require another newly delivered
        # frame with unchanged target/pads and unchanged arm before committing.
        before = self.wrist_image
        decoded, fresh = self._next_wrist_hsv(before, timeout=0.5)
        after = self.manipulation.pose
        if (
            decoded is None
            or fresh is None
            or fresh is before
            or not all(math.isfinite(v) for v in (*after.position, *after.rpy))
            or math.dist(pose.position, after.position) > 0.002
            or max(abs(a - b) for a, b in zip(pose.rpy, after.rpy, strict=True)) > 0.01
            or not same_wrist_patch(base64.b64decode(frame), base64.b64decode(fresh), verdict[0]["box_2d"])
        ):
            raise SkillFailed("Wrist endpoint changed during the final decision")
        j6 = self._arm_joints()[5]
        if not math.isfinite(j6) or j6 < self.manipulation.GRIPPER_OPEN - 0.05:
            raise SkillFailed("Metric endpoint claw is not fully open")
        self.check_cancelled()
        # Retain the ordinary close/lift/encoder checks. A confirmed empty close
        # cannot retry through the unrelated image tracker in this prototype.
        original_params = self._p
        self._p = {**original_params, "grasp_retries": 0.0}
        try:
            self._close_twist_lift(prompt, x, y, roll, pitch, yaw)
        finally:
            self._p = original_params

    def _grasp_verified(self, prompt, approach: FloorApproach):
        """Verify a lifted grasp, backing up only when mechanics are unclear.

        A non-empty claw after the lift, with the end effector measurably clear
        of the floor, is already strong evidence of a grasp.  Do not drive the
        base away in that state: a rigid object can be secure vertically but
        get knocked loose by the horizontal verification move.  The existing
        camera check remains the fallback for missing or ambiguous telemetry.
        """
        js = self.joint_states
        j6 = js.position[5] if js is not None and len(js.position) > 5 else None
        j6_ok = j6 is not None and j6 > GRIPPER_EMPTY_J6 + 0.02
        try:
            ee_z = self.manipulation.pose.z
        except ArmFailed:
            ee_z = None
        lifted_clear = ee_z is not None and ee_z >= self._p["floor_z"] + 0.07
        if j6_ok and lifted_clear:
            self.logger.info(f"[PickAnyObject] verify: lifted grasp j6={j6} z={ee_z:.3f} -> HELD")
            return True

        approach.drive(-VERIFY_BACKUP_M)
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

    def execute(self, prompt: str = "the sock", controller: str = "astra") -> SkillReturn:
        """Pick up `prompt` from the floor."""
        if self._proxy is None:
            self.fail("Innate proxy not configured (INNATE_SERVICE_KEY)")
        if controller not in {"astra", "classic", "rgbd"}:
            self.fail("Pickup controller must be astra, classic or rgbd")
        self._metric_pickup = controller == "rgbd"
        self._metric_reference = None
        self._pickup_policy = None
        self._planned_roll = None
        self._search_clearance = "high"
        self._head_reference = None
        self._nav_pending = False
        if controller in {"astra", "rgbd"}:
            from innate_skills.pickup_policy import PickupPolicy

            from brain_client.brain.openai_transport import pick_openai_transport

            transport, _backend = pick_openai_transport(self._proxy)
            if transport is None:
                self.fail("Astra pickup requires OpenAI access")
            self._pickup_policy = PickupPolicy(
                transport, record=lambda **values: self.logger.info(f"[PickAnyObject] Astra usage: {values}")
            )

        # Per-run reset: don't carry the last run's object or grip rating.
        self._grip_strength = None
        self._holding = False
        self._last_seen = None
        self._coasts = 0
        try:
            self.head.set_position(int(round(self._p["tilt_deg"])))
            # Fold clear of the head camera before searching
            # (5-joint move: the claw keeps whatever it currently holds).
            if self._pickup_policy is None:
                self.manipulation.move_joints(NAV_ARM, duration=3.0)
            else:
                self.manipulation.move_joints(NAV_ARM, duration=3.0, block=False)
                self._nav_pending = True

            approach = FloorApproach(
                self,
                self._p,
                self._detect_px,
                # Astra's wrist stage must recognize the target again before
                # any grasp. Blind or classic callers keep head confirmation.
                confirm_arrival=self._pickup_policy is None or self._p["wrist_steps"] < 1,
            )
            self.say(f"Looking for {prompt}.")
            xy = approach.search(prompt)
            xy = approach.position_above(prompt, xy)
            self.say("Picking it up.")
            self._grasp_at(prompt, xy)
            # _close_twist_lift latched self._holding the moment the fingers
            # committed — only a verified miss clears it.
            if not self._grasp_verified(prompt, approach):
                self._holding = False
                self.say("I couldn't get a grip on it.")
                raise SkillFailed(f"Grasp missed — '{prompt}' is still on the floor (verified after backing up)")
        except ArmFailed as e:
            # A clean arm give-up is a skill failure, not a crash. SkillFailed
            # and SkillCancelled propagate untouched — the framework owns them.
            self.fail(str(e))
        except ArmUnhealthy as e:
            self.say("My arm isn't responding properly, stopping.")
            self.fail(f"Arm servo failure: {e}")
        finally:
            self.mobility.stop()
            try:
                self._finish_nav_fold()
            except (ArmFailed, ArmUnhealthy):
                self.logger.warning("[PickAnyObject] navigation fold did not complete")
            self._rest_arm(keep_grip=self._holding)
            self.head.set_position(0)
        # A rigid object can slip during the final fold even after a verified
        # lift. Report success only after that motion, using the encoder again.
        if self._gripper_closed_on_air():
            self._holding = False
            self.fail(f"'{prompt}' slipped while moving to the carry pose. Ask me to pick it up again.")
        self.say("Got it.")
        return f"Picked up '{prompt}' (grip verified after the lift and carry motion)"
