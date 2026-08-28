# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Drop a held object into a box, bin or basket.

Reuses pick_any_object's approach layer (_FloorApproach) with two changes a
container forces: the localized pixel is the box's BOTTOM edge, not its centre
— a tall object's centre back-projects far past where it stands — and the base
parks short of the box instead of over it. The release itself is a hover and a
gripper_open.
"""

import math
import re
import time

from innate_skills.approach import APPROACH_PARAMS, _FloorApproach

from innate import Head, JointStates, Manipulation, SkillReturn, WristImage, vision
from innate import gemini as gemlib
from innate.exceptions import ArmFailed, ArmUnhealthy, SkillFailed
from innate.geometry import IMG_H, IMG_W, pixel_to_floor, pixel_to_height

# Arm reach as a sphere about the shoulder, from the URDF: joint2 sits at
# (0.086, 0.0845) in base_link and 0.326 m of link follows it. The 0.086 m
# forward offset is the whole reason a drop over a rim is possible at all.
# Sanity check on the model: it puts the floor-height limit at 0.407 m, which
# is where Manipulation.REACH_X's hardcoded 0.40 comes from.
SHOULDER_X, SHOULDER_Z = 0.086, 0.0845
ARM_REACH = 0.326

# Claw angle band that means "something is between the fingers": above the
# closed-on-air floor (pick_any_object's GRIPPER_EMPTY_J6) and below a claw
# sitting at its commanded-open limit, which is holding nothing.
HOLDING_J6 = (-0.065, 0.80)

VERIFY_BACKUP_M = 0.15

# Every arm move below that happens while the object is held passes
# tolerance_xy=None, tolerance_z=None. This is load-bearing, not tidiness:
# move_to's VERIFIED path answers a missed pose with Manipulation.recover,
# which reboots the servos with torque off — that opens the fingers and drops
# whatever is being carried, and only the standing grip TARGET survives the
# reboot, so the code afterwards still believes it is holding. Harmless
# mid-pick with an empty claw, a guaranteed drop while carrying.

# A detection touching the top of the frame has had its rim cropped away, so
# any height read off it is meaningless — at the park distance the container
# fills the view and both the bbox top and the model's rim line sit at row 0.
CLIP_MARGIN_PX = 3.0

PARAMS = {
    **APPROACH_PARAMS,
    # The box's near floor edge parks here. Bounded below by the 0.25 m front
    # bumper (costmap footprint) and above by the 0.40 m reach clamp, which has
    # to also fit drop_inset — the window is barely 5 cm wide.
    "sweet_x": 0.30,
    # Bearing: loose. Close in a container runs off the frame edges and its
    # bbox centre stops tracking its middle; 66 px is ~50 mm of lateral error
    # here, against a 316 mm interior. Range: TIGHTER than pick's, because one
    # image row is ~1.2 cm of range at 0.30 m and the whole usable window
    # between the bumper and the reach limit is about 5 cm. 18 px is ~2.2 cm.
    "box_half_px": 110.0,
    "box_half_v_px": 30.0,
    "accept_frac": 0.6,
    # How far past the near face the gripper reaches. 0.07 released only 5.8 cm
    # inside a 31.6 cm interior — hugging the near wall, which is where a
    # bounce goes back out. 0.10 asks for 8.1 cm and the reach clamp below
    # trims it to whatever the arm can actually hold at the release height.
    "drop_inset": 0.10,
    # Gripper height above the rim at release. The arm stays here and simply
    # opens — it does not follow the object down. At arm_pitch the claw already
    # points down, so the fingertips hang below the rim at this height and the
    # object is inside the container before it is let go; reaching further down
    # only drives them toward the container's floor.
    "release_clear_m": 0.04,
    "release_settle_s": 0.8,
    # Travel pose: the object rides here for the drive. Swung to the RIGHT and
    # held high — base_link +y is left, so the camera's right is -y. Chosen
    # against three constraints at once: 83% of the arm's reach (the same
    # margin the old straight-ahead pose had), |y| inside the 0.165 m nav
    # footprint so nothing sticks out past the robot's own width, and a
    # projection of u=893, v=-185 — off the right edge AND above the top of a
    # 640x480 frame, so it cannot occlude the floor being searched.
    "travel_x": 0.18,
    "travel_y": -0.15,
    "travel_z": 0.32,
    # Where the object is lifted to at the container before reaching over the
    # rim, at the release bearing.
    "carry_x": 0.24,
    "carry_z": 0.30,
    "carry_s": 2.0,
    # Re-squeeze before driving. move_to only carries the STANDING grip target
    # forward, which is whatever the last motion happened to command — run
    # standalone, or after anything that touched the claw, that can be a
    # target the object has already worked loose from. GRIPPER_MAX_STRENGTH;
    # above it the real servo overcurrent-trips.
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
    # Rim estimate band. Under the floor the geometry is noise; over the top
    # no release pose fits — at sweet_x + drop_inset_min the envelope runs out
    # at a rim of about 0.22, which is where this number comes from.
    "rim_z_min": 0.04,
    "rim_z_max": 0.22,
}


class DropInBox(_FloorApproach):
    """Put an object the robot is ALREADY HOLDING into a container — a box,
    bin, basket or crate (prompt='the cardboard box', 'the laundry basket').
    The robot finds the container with its head camera, drives up to it,
    reaches over the rim and opens the gripper. Run pick_any_object first;
    this fails immediately if the gripper is empty. Only works for containers
    low enough for the arm to reach over — roughly shoebox height."""

    manipulation: Manipulation
    head: Head
    # `| None` — best effort: the hold check falls back to the wrist camera
    # without joint states, and to the joints without wrist frames.
    wrist_image: WristImage | None
    joint_states: JointStates | None

    _p = PARAMS
    # The last detection, as the two scalars _rim_height needs. Kept apart
    # rather than as the box tuple: a subscripted generic in a class-level
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

        The bottom edge midpoint, NOT the centre: pixel_to_floor intersects the
        z=0 plane, so back-projecting a point at rim height reports the box far
        past where it stands (a 0.15 m rim at 0.6 m reads as 0.88 m)."""
        text, img = self._ask_head(
            f"Find '{prompt}' in this image — an open container (box, bin, basket, crate) "
            "standing on the floor. Ignore the robot's own gripper and anything it is "
            "holding. Return ONLY a JSON list of matches, each "
            '{"box_2d":[ymin,xmin,ymax,xmax], "near_rim_y":y} normalized 0-1000, best '
            "first. box_2d is TIGHT around the whole container, including where it rests "
            "on the floor. near_rim_y is the image row of the TOP EDGE OF THE WALL "
            "NEAREST THE CAMERA — the front rim you would reach over, not the far rim "
            "behind it. Empty list if no container is visible."
        )
        box = vision.parse_det_box(text)
        self._near_rim_v = _near_rim_v(text)
        if box is None:
            self._box_u = self._box_top_v = None
            self._debug("detect", image=img, label=prompt, note="no container")
            return None
        x, y, w, h = box
        self._box_u = min(float(IMG_W - 1), x + w / 2.0)
        self._box_top_v = float(y)
        px = (self._box_u, min(float(IMG_H - 1), float(y + h)))
        self._measure_rim(px)
        self._debug("detect", image=img, label=prompt, box_px=list(box), point_px=list(px), near_rim_v=self._near_rim_v)
        return px

    def _measure_rim(self, px: tuple[float, float]) -> None:
        """Bank the rim height from THIS detection, while the container is
        still whole in frame. Height is a property of the box, not of where
        the robot stands, so an estimate taken at range survives the approach
        — by the time the base has parked the rim is cropped off the top of
        the image and cannot be measured at all."""
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
        self._debug("rim", rim_z=rim, measured=self._rim_z is not None, near_x=near_x)
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

    def _holding(self) -> bool:
        """The gripper has something in it. The wrist camera decides whenever
        it has a view: j6 cannot tell a grip from a claw merely parked shut —
        REST leaves it at 0.003, inside the band a held object produces — and
        that false positive sends the robot off to flail at a container with
        an empty gripper. The joints are the fallback."""
        j6 = self._j6()
        by_joint = j6 is not None and HOLDING_J6[0] < j6 < HOLDING_J6[1]
        seen = self._wrist_sees_object()
        held = by_joint if seen is None else seen
        self.logger.info(f"[DropInBox] hold check: j6={j6} joints={by_joint} wrist={seen} -> {held}")
        self._debug("hold", j6=j6, by_joint=by_joint, wrist=seen, held=held)
        return held

    def _secure_grip(self) -> None:
        """Re-close the claw before the drive. move_to only carries the
        STANDING grip target forward, which is whatever the last motion
        happened to command — run standalone, or after anything that touched
        the claw, that can be a target the object has already worked loose
        from. Best effort: a refused close is worth a warning, not a failed
        run."""
        p = self._p
        self.manipulation.torque_on()
        try:
            self.manipulation.gripper_close(p["carry_grip"], duration=p["carry_grip_s"])
        except (ArmFailed, ArmUnhealthy) as e:
            self.logger.warning(f"[DropInBox] could not re-grip before the drive ({e})")
        self._debug("grip", j6=self._j6())

    def _carry_for_travel(self) -> None:
        """Hold the object high and out to the right for the drive.

        The rest fold carries it too low. Manipulation.rest is the fallback
        rather than the choice: it keeps the grip (standing target substituted
        for REST's own j6) and is known to be safe under load, so a pose this
        arm will not solve still leaves the object held and the run going."""
        p = self._p
        try:
            self.manipulation.move_to(
                p["travel_x"],
                p["travel_y"],
                p["travel_z"],
                pitch=p["arm_pitch"],
                duration=p["carry_s"],
                tolerance_xy=None,
                tolerance_z=None,
            )
            self._debug("travel", target_xyz=[p["travel_x"], p["travel_y"], p["travel_z"]], j6=self._j6())
            return
        except (ArmFailed, ArmUnhealthy) as e:
            self.logger.warning(f"[DropInBox] travel pose refused ({e}); folding to rest instead")
        try:
            self.manipulation.rest(duration=p["carry_s"])
        except (ArmFailed, ArmUnhealthy) as e:
            self.logger.warning(f"[DropInBox] could not fold for the drive either ({e}); driving as-is")
        self._debug("travel", note="fell back to rest", j6=self._j6())

    def _lift_clear(self, y: float) -> None:
        """Raise the object above rim height, at the release bearing, so the
        reach over the near wall comes down from above rather than straight
        through it. Best effort — the object is still held either way."""
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
        self._debug("lift", target_xyz=[p["carry_x"], y, p["carry_z"]], j6=self._j6())

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
        self._debug("release", target_xyz=[x, y, z], rim_z=rim, reach_x_max=reach_x_max(z))

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
        self._debug("lift_out", over_rim=self._over_rim)

    def _retract(self) -> None:
        """Best-effort teardown: straight up off the rim, then fold. Never
        raises. Lifting first matters — REST swings the gripper forward and
        down, which would drag it through the container wall.

        A run that never released folds through Manipulation.rest, which keeps
        the grip: REST's 6th element is a claw command, so folding to it
        outright would open the fingers and drop the object on the floor."""
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

    def _landed(self, prompt: str) -> bool:
        """Back up, then ask both cameras whether the object went in."""
        self._drive(-VERIFY_BACKUP_M)
        self.sleep(self._p["settle_s"])
        main_img, wrist_img = self.main_image, self.wrist_image
        images = [img for img in (main_img, wrist_img) if img]
        if not images:
            j6 = self._j6()
            still_held = j6 is not None and HOLDING_J6[0] < j6 < HOLDING_J6[1]
            self._debug("verify", reply=None, landed=not still_held, j6=j6, note="no cameras")
            return not still_held
        labels = []
        if main_img:
            labels.append(f"Image {len(labels) + 1} is the head camera looking at the floor.")
        if wrist_img:
            labels.append(f"Image {len(labels) + 1} is the WRIST camera beside the gripper fingers (mirrored).")
        text = gemlib.ask_image(
            self._proxy,
            images,
            f"The robot just tried to drop an object into '{prompt}'. {' '.join(labels)} "
            "Did the object end up INSIDE the container? Answer NO if it is lying on the "
            "floor beside the container, or still held in the gripper. Answer only YES or NO.",
            logger=self.logger,
        )
        verdict = _yes_no(text)
        self.logger.info(f"[DropInBox] landed: reply={text!r} -> {verdict}")
        self._debug("verify", reply=text, landed=verdict, image=main_img)
        # No usable verdict is not a failure: the claw is open and the object
        # is no longer held, which is as much as this skill promised.
        return verdict is not False

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
            # The 50 Hz state feeds only start with the run, so the very first
            # read is empty — judging the grip there reports a genuinely
            # carried object as an empty claw and refuses to move.
            self.wait_for(lambda: self.joint_states, timeout=3.0)
            if not self._holding():
                self.fail("I'm not holding anything to put away")

            self._secure_grip()
            self._carry_for_travel()
            self.say(f"Looking for {prompt}.")
            xy = self._search(prompt)
            xy = self._position_above(prompt, xy)
            self.say("Dropping it in.")
            _x, _y, released_z = self._release_at(xy[0], xy[1])

            if not self._landed(prompt):
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
