# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Drop a held object into a box, bin or basket. Built on _FloorApproach; a
container changes two things: localize by its BOTTOM edge (a rim-height point
back-projects far past the box) and park short of it rather than over it.
"""

import math
import re
import time

from innate_skills.approach import APPROACH_PARAMS, FloorApproach, ask_head

from innate import (
    Head,
    JointStates,
    MainImage,
    Manipulation,
    Mobility,
    Odometry,
    Skill,
    SkillReturn,
    WristImage,
    resource,
    vision,
)
from innate import gemini as gemlib
from innate.exceptions import ArmFailed, ArmUnhealthy, SkillFailed
from innate.geometry import IMG_H, IMG_W, floor_to_pixel, pixel_to_floor, pixel_to_height

# Arm reach as a sphere about the shoulder (URDF: joint2 at (0.086, 0.0845),
# 0.326 m of link past it). It predicts a 0.407 m floor-height limit — where
# Manipulation.REACH_X's 0.40 comes from.
SHOULDER_X, SHOULDER_Z = 0.086, 0.0845
ARM_REACH = 0.326

# j6 band that PROVES a hold on its own, valid only after a fresh close.
# Below 0.05 the joints cannot tell a squeezed-thin object from a claw that
# silently never moved off its parked 0.003 — there the wrist camera decides.
HOLDING_J6 = (0.05, 0.80)

VERIFY_BACKUP_M = 0.15

# Every arm move made while holding passes tolerance_xy/z=None: the verified
# path answers a missed pose with Manipulation.recover, which reboots the
# servos torque-off — the fingers open and the object drops.

# A detection touching the frame top has its rim cropped: any height read off
# it is meaningless.
CLIP_MARGIN_PX = 3.0

PARAMS = {
    **APPROACH_PARAMS,
    # -12 sees floor from 0.19 m out, so the 0.23 park sits 40 px above the
    # frame bottom; past it the contact line leaves frame (see _park_if_clipped).
    "tilt_deg": -12.0,
    # Near floor edge parks here. Close on purpose: at 0.33 the reach clamp
    # trimmed every release to a rim hug; still clear of the body (the
    # shoulder at 0.086 is the frontmost part).
    "sweet_x": 0.23,
    # Bearing loose (a close container runs off the frame edges). Range was
    # 12 px — a 16 mm accept band the servo cannot resolve: the feed is 7.5 Hz
    # and drive_v_min 0.04 m/s, so its smallest blind step is ~6 mm and it
    # limit-cycled instead of parking. 25 px gives a 34 mm deadband (~6 steps)
    # and a 57 mm hold band, both well inside the 0.15-0.30 m park that
    # _release_x tolerates at every rim in the band.
    "box_half_px": 50.0,
    "box_half_v_px": 25.0,
    "accept_frac": 0.6,
    # Reach past the near face. 0.03 draped a sock on the rim; the reach
    # clamp trims tall rims down to drop_inset_min at worst.
    "drop_inset": 0.08,
    # Release height above the rim; the arm opens here, no descent. At
    # arm_pitch the fingertips already hang below the rim, so the object is
    # inside before it is let go.
    "release_clear_m": 0.04,
    "release_settle_s": 0.8,
    # Posed live on hardware for least head-camera occlusion (2026-08-28).
    # 5 joints; move_joints carries the standing grip as j6.
    "travel_joints": [-1.72, 0.31, -1.84, 1.70, -0.26],
    # Where the object is lifted to at the container before reaching over the
    # rim, at the release bearing.
    "carry_x": 0.24,
    "carry_z": 0.30,
    "carry_s": 2.0,
    # Re-squeeze before driving: move_to carries only the standing grip target,
    # which the object may have worked loose from. GRIPPER_MAX_STRENGTH; above
    # it the real servo overcurrent-trips.
    "carry_grip": 0.60,
    "carry_grip_s": 0.8,
    "lift_after_m": 0.08,  # the fingertips hang below the rim even at hover
    "hover_s": 2.5,
    "arm_pitch": 1.30,
    # Least the gripper may sit past the near face and still be over the
    # interior. Below this the object would land on the rim.
    "drop_inset_min": 0.03,
    # Keep off the kinematic edge: pick already notes that working at the 0.40
    # reach limit stalls the wrist and overloads servo 2.
    "reach_margin": 0.03,
    # Rim estimate band: below it the geometry is noise; 0.22 is the tallest
    # rim the release was ever proven on.
    "rim_z_min": 0.04,
    "rim_z_max": 0.22,
}


class DropInBox(Skill):
    """Put an object the robot is ALREADY HOLDING into a container — a box,
    bin, basket or crate (prompt='the cardboard box', 'the laundry basket').
    The robot finds the container with its head camera, drives up to it,
    reaches over the rim and opens the gripper. Run pick_any_object first;
    this fails immediately if the gripper is empty. Only works for containers
    low enough for the arm to reach over — roughly shoebox height.
    Call this only if the box is actually visible, otherwise search for it
    first by looking around."""

    manipulation: Manipulation
    mobility: Mobility
    head: Head
    main_image: MainImage | None
    odom: Odometry | None
    # `| None` — best effort: the hold check falls back to the wrist camera
    # without joint states, and to the joints without wrist frames.
    wrist_image: WristImage | None
    joint_states: JointStates | None

    _p = PARAMS

    @resource
    def _proxy(self):
        return gemlib.make_client()

    # Two scalars, not the box tuple: a subscripted generic in a class-level
    # annotation crashes the feed-annotation machinery at import.
    _box_u: float | None = None
    _box_top_v: float | None = None
    _near_rim_v: float | None = None
    _rim_z: float | None = None
    _label = "the box"
    _over_rim = False  # the gripper is inside the container's footprint
    _released = False  # the object has left the claw

    def _detect_px(self, prompt: str) -> tuple[float, float] | None:
        """Head frame -> the container's near floor-contact pixel, or None.
        Bottom edge midpoint, NOT the centre: a point at rim height
        back-projects far past the box (a 0.15 m rim at 0.6 m reads 0.88)."""
        text, img = ask_head(
            self,
            self._proxy,
            f"Find '{prompt}' in this image — an open container (box, bin, basket, crate) "
            "standing on the floor. Ignore the robot's own gripper and anything it is "
            "holding. Return ONLY a JSON list of matches, each "
            '{"box_2d":[ymin,xmin,ymax,xmax], "near_rim_y":y} normalized 0-1000, best '
            "first. box_2d is TIGHT around the whole container, including where it rests "
            "on the floor. near_rim_y is the image row of the TOP EDGE OF THE WALL "
            "NEAREST THE CAMERA — the front rim you would reach over, not the far rim "
            "behind it. Empty list if no container is visible.",
            self._p["settle_s"],
        )
        box = vision.parse_det_box(text)
        self._near_rim_v = _near_rim_v(text)
        if box is None:
            self._box_u = self._box_top_v = None
            return None
        x, y, w, h = box
        self._box_u = min(float(IMG_W - 1), x + w / 2.0)
        self._box_top_v = float(y)
        px = (self._box_u, min(float(IMG_H - 1), float(y + h)))
        self._measure_rim(px)
        return self._park_if_clipped(px, float(y + h))

    def _park_if_clipped(self, px: tuple[float, float], contact_v: float) -> tuple[float, float]:
        """A floor-contact row at or past the frame bottom means the container
        is nearer than the 0.19 m this tilt can see — at or past the park, not
        somewhere measurable. Clamped to the last row it back-projects to a
        fixed 0.19 that reads 'too close' forever, walking the base backwards
        and burning a step each time; report the park row and keep only the
        bearing this detection can still see."""
        if contact_v < IMG_H - 1:
            return px
        park = floor_to_pixel(self._p["sweet_x"], self._p["box_y"], self._p["tilt_deg"])
        if park is None:
            return px
        self.logger.info(f"[DropInBox] contact row clipped at v={contact_v:.0f}; treating as parked")
        return (px[0], park[1])

    def _measure_rim(self, px: tuple[float, float]) -> None:
        """Bank the rim height while the container is still whole in frame:
        parked, the rim is cropped off the top and cannot be measured."""
        v = self._near_rim_v if self._near_rim_v is not None else self._box_top_v
        if v is None or v <= CLIP_MARGIN_PX or self._box_top_v is None or self._box_top_v <= CLIP_MARGIN_PX:
            return
        xy = pixel_to_floor(px[0], px[1], self._p["tilt_deg"])
        if xy is None or self._box_u is None:
            return
        raw = pixel_to_height(self._box_u, v, self._p["tilt_deg"], xy[0])
        if raw is None:
            return
        self._rim_z = max(self._p["rim_z_min"], min(self._p["rim_z_max"], raw))
        self.logger.info(f"[DropInBox] rim {self._rim_z:.3f} (raw={raw:.3f}) measured at range {xy[0]:.2f}")

    def _rim_height(self, near_x: float) -> float:
        """The rim height banked by _measure_rim, or the floor of the band if
        no detection ever caught the container uncropped."""
        rim = self._rim_z if self._rim_z is not None else self._p["rim_z_min"]
        return rim

    def _j6(self) -> float | None:
        js = self.joint_states
        return js.position[5] if js is not None and len(js.position) > 5 else None

    def _wrist_sees_object(self) -> bool | None:
        """Wrist-camera opinion on whether the fingers hold something.
        None when there is no frame or no verdict."""
        img = self.wrist_image
        if not img:
            return None
        text = gemlib.ask_image(
            self._proxy,
            img,
            "Wrist camera mounted beside a robot gripper's fingers (the view is mirrored). "
            "Are the fingers holding an object right now? Answer only YES or NO.",
            logger=self.logger,
        )
        return _yes_no(text)

    def _holding(self, closed: bool) -> bool:
        """The gripper has something in it: j6 clearly at an object's width
        after a fresh close, or the wrist camera seeing one. Neither a failed
        close nor a low j6 counts alone."""
        j6 = self._j6()
        by_joint = closed and j6 is not None and HOLDING_J6[0] < j6 < HOLDING_J6[1]
        seen = self._wrist_sees_object()
        held = by_joint or seen is True
        self.logger.info(f"[DropInBox] hold check: j6={j6} joints={by_joint} wrist={seen} -> {held}")
        return held

    def _secure_grip(self) -> bool:
        """Re-close the claw before the drive; True if the close went through
        (_holding trusts j6 only against a fresh close)."""
        p = self._p
        self.manipulation.torque_on()
        try:
            self.manipulation.gripper_close(p["carry_grip"], duration=p["carry_grip_s"])
        except (ArmFailed, ArmUnhealthy) as e:
            self.logger.warning(f"[DropInBox] could not re-grip before the drive ({e})")
            return False
        return True

    def _carry_pose(self, joints: list[float]) -> None:
        """Move the held object to a 5-joint pose (grip kept); a refused pose
        falls back to Manipulation.rest, also grip-preserving."""
        try:
            self.manipulation.move_joints(joints, duration=self._p["carry_s"])
            return
        except (ArmFailed, ArmUnhealthy) as e:
            self.logger.warning(f"[DropInBox] carry pose refused ({e}); folding to rest instead")
        try:
            self.manipulation.rest(duration=self._p["carry_s"])
        except (ArmFailed, ArmUnhealthy) as e:
            self.logger.warning(f"[DropInBox] could not fold either ({e}); carrying on as-is")

    def _lift_clear(self, y: float) -> None:
        """Raise the object above rim height at the release bearing, so the
        reach comes down from above the near wall, not through it."""
        p = self._p
        try:
            self.manipulation.move_to(
                p["carry_x"],
                y,
                p["carry_z"],
                pitch=p["arm_pitch"],
                duration=p["carry_s"],
                tolerance_xy=None,
                tolerance_z=None,
            )
        except (ArmFailed, ArmUnhealthy) as e:
            self.logger.warning(f"[DropInBox] could not lift clear of the rim ({e}); reaching from where it is")

    def _release_x(self, near_x: float, z: float) -> float:
        """How far forward the gripper may hover at height `z`, or raise if the
        rim is high enough that nothing over the interior is still in reach."""
        p = self._p
        limit = reach_x_max(z)
        limit = None if limit is None else limit - p["reach_margin"]
        floor = near_x + p["drop_inset_min"]
        if limit is None or limit < floor:
            raise SkillFailed(
                f"'{self._label}' is too tall to reach over — its rim needs the gripper at "
                f"z={z:.2f} m, where the arm only reaches x={limit or 0.0:.2f} m and the "
                f"container's near wall is already at x={near_x:.2f} m"
            )
        return min(near_x + p["drop_inset"], limit)

    def _release_at(self, near_x: float, near_y: float) -> tuple[float, float, float]:
        """Hover over the container interior and open the claw."""
        p = self._p
        rim = self._rim_height(near_x)
        z = rim + p["release_clear_m"]
        x, y = self.manipulation.clamp_reach(self._release_x(near_x, z), near_y)

        self.manipulation.torque_on()
        self._lift_clear(y)
        try:
            self.manipulation.move_to(
                x, y, z, pitch=p["arm_pitch"], duration=p["hover_s"], tolerance_xy=None, tolerance_z=None
            )
        except ArmFailed as e:
            raise SkillFailed(
                f"Could not hold the gripper over the rim at z={z:.2f} m — "
                f"the container looks too tall for this arm ({e})"
            ) from e
        # Latched BEFORE the claw opens: a cancel during the settle below must
        # still lift the fingers out of the container, and an unlatched flag
        # folded REST straight through the wall.
        self._over_rim = True

        self.check_cancelled()  # last exit before the object leaves the claw
        self.manipulation.gripper_open(duration=1.0)
        self._released = True
        self.sleep(p["release_settle_s"])
        # Out of the container BEFORE anything drives: the verification backs
        # the base up 0.15 m, and doing that with the claw still hooked over
        # the rim drags the container along with it.
        self._lift_out()
        return x, y, z

    def _lift_out(self) -> None:
        """Straight up until the fingers clear the rim. Never raises; clears
        the over-rim latch only on success, so a failed lift still tells
        teardown the gripper is in the container."""
        try:
            self.manipulation.move_by(dz=self._p["lift_after_m"], duration=1.0, tolerance_xy=None, tolerance_z=None)
            self._over_rim = False
        except (ArmFailed, ArmUnhealthy) as e:
            self.logger.warning(f"[DropInBox] could not lift clear of the container ({e})")

    def _retract(self) -> None:
        """Best-effort teardown, never raises: straight up off the rim (REST
        swings the gripper forward and down, through the wall), then fold. A
        run that never released folds via Manipulation.rest with the grip kept —
        REST's 6th element is a claw command."""
        try:
            if self._over_rim:
                # Only reached when the normal lift-out never ran (a cancel
                # mid-release, or a lift that failed). Committed teardown:
                # time.sleep on purpose.
                self.manipulation.move_by(dz=self._p["lift_after_m"], duration=1.0, tolerance_xy=None, tolerance_z=None)
                time.sleep(0.3)
            if self._released:
                self.manipulation.move_joints(self.manipulation.REST, duration=3.0)
            else:
                self.manipulation.rest(duration=3.0)  # keeps the grip on a run that never released
        except Exception as e:  # noqa: BLE001 — teardown must not mask the run result
            self.logger.warning(f"[DropInBox] retract failed: {e}")

    def _landed(self, prompt: str, approach: FloorApproach) -> bool:
        """Back up, then look for evidence the object did NOT go in."""
        approach.drive(-VERIFY_BACKUP_M)
        self.sleep(self._p["settle_s"])
        main_img, wrist_img = self.main_image, self.wrist_image
        images = [img for img in (main_img, wrist_img) if img]
        if not images:
            j6 = self._j6()
            still_held = j6 is not None and HOLDING_J6[0] < j6 < HOLDING_J6[1]
            return not still_held
        labels = []
        if main_img:
            labels.append(f"Image {len(labels) + 1} is the head camera looking at the floor.")
        if wrist_img:
            labels.append(f"Image {len(labels) + 1} is the WRIST camera beside the gripper fingers (mirrored).")
        # Burden of proof on FAILURE: a successful drop is usually invisible
        # (below the rim, behind the near wall) — affirming "inside" produced
        # false misses on tall boxes. Only seeing the object outside counts.
        text = gemlib.ask_image(
            self._proxy,
            images,
            f"The robot just dropped an object into '{prompt}'. {' '.join(labels)} "
            "Can you SEE the dropped object OUTSIDE the container — lying on the floor "
            "next to it, or still between the gripper fingers? A successful drop leaves "
            "the object hidden inside the container, so if you cannot see it anywhere, "
            "answer NO. Answer only YES or NO.",
            logger=self.logger,
        )
        missed = _yes_no(text)
        self.logger.info(f"[DropInBox] landed: reply={text!r} -> {missed is not True}")
        # No usable verdict is not a failure: the claw is open and the object
        # is no longer held, which is as much as this skill promised.
        return missed is not True

    def execute(self, prompt: str = "the box") -> SkillReturn:
        """Drop whatever the gripper holds into `prompt`."""
        if self._proxy is None:
            self.fail("Innate proxy not configured (INNATE_SERVICE_KEY)")

        self._box_u = self._box_top_v = None
        self._near_rim_v = None
        self._rim_z = None
        self._over_rim = False
        self._released = False
        self._label = prompt
        released_z = None
        try:
            self.head.set_position(int(round(self._p["tilt_deg"])))
            # The 50 Hz feeds start with the run: the first read is empty and
            # would report a held object as an empty claw.
            self.wait_for(lambda: self.joint_states, timeout=3.0)
            # Close before judging: _holding reads j6 against a fresh close.
            closed = self._secure_grip()
            if not self._holding(closed):
                if not closed:
                    self.fail("The gripper isn't responding, so I can't tell whether I'm holding anything")
                self.fail("I'm not holding anything to put away (or can't confirm it without a wrist view)")

            self._carry_pose(self._p["travel_joints"])
            approach = FloorApproach(self, self._p, self._detect_px)
            self.say(f"Looking for {prompt}.")
            xy = approach.search(prompt)
            xy = approach.position_above(prompt, xy)
            self.say("Dropping it in.")
            _x, _y, released_z = self._release_at(xy[0], xy[1])

            if not self._landed(prompt, approach):
                self.say("It missed the box.")
                raise SkillFailed(f"Released, but the object did not land in '{prompt}'")
            self.say("Done.")
            rim = f"{self._rim_z:.2f}" if self._rim_z is not None else "?"
            return f"Dropped the object into '{prompt}' (rim ~{rim} m, released at z={released_z:.2f})"
        except ArmFailed as e:
            self.fail(str(e))
        except ArmUnhealthy as e:
            self.say("My arm isn't responding properly, stopping.")
            self.fail(f"Arm servo failure: {e}")
        finally:
            self.mobility.stop()
            self._retract()
            self.head.set_position(0)


def reach_x_max(z: float) -> float | None:
    """Furthest forward (base_link x) the gripper reaches at height `z`."""
    span = ARM_REACH**2 - (z - SHOULDER_Z) ** 2
    return SHOULDER_X + math.sqrt(span) if span > 0 else None


def _near_rim_v(text: str | None) -> float | None:
    """The best detection's near_rim_y as an image row, or None."""
    for det in vision.parse_dets(text):
        y = det.get("near_rim_y")
        if isinstance(y, (int, float)) and not isinstance(y, bool) and math.isfinite(y):
            return min(1000.0, max(0.0, float(y))) / 1000.0 * IMG_H
    return None


def _yes_no(text: str | None) -> bool | None:
    """YES/NO as a whole-word scan; None when the reply is empty, hedged or
    says both. Anchored prefix matching read 'It is not in the box.' as a yes."""
    verdict = (text or "").upper()
    yes = re.search(r"\bYES\b", verdict) is not None
    no = re.search(r"\bNO\b", verdict) is not None
    return None if yes == no else yes
