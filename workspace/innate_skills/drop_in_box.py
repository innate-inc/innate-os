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
from innate.geometry import IMG_H, IMG_W, pixel_to_height

Box = tuple[int, int, int, int]

# Claw angle band that means "something is between the fingers": above the
# closed-on-air floor (pick_any_object's GRIPPER_EMPTY_J6) and below a claw
# sitting at its commanded-open limit, which is holding nothing.
HOLDING_J6 = (-0.065, 0.80)

VERIFY_BACKUP_M = 0.15

PARAMS = {
    **APPROACH_PARAMS,
    # The box's near floor edge parks here. Bounded below by the 0.25 m front
    # bumper (costmap footprint) and above by the 0.40 m reach clamp, which has
    # to also fit drop_inset — the window is barely 5 cm wide.
    "sweet_x": 0.30,
    # How far past the near face the gripper hovers, so the object clears the
    # box wall on the way down.
    "drop_inset": 0.07,
    "release_clear_m": 0.05,  # gripper height above the rim at release
    "release_settle_s": 0.8,
    "lift_after_m": 0.04,
    "hover_s": 2.5,
    "arm_pitch": 1.30,
    # Rim estimate band. Below the floor of this the geometry is noise; above
    # its top the arm cannot hold a pose over the rim at all (see drop_z_max).
    "rim_z_min": 0.04,
    "rim_z_max": 0.22,
    # Ceiling on the release height. The arm's true envelope at this reach is
    # unmeasured — probe it before raising this, or IK just refuses the pose.
    "drop_z_max": 0.26,
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
    _last_box: Box | None = None
    _near_rim_v: float | None = None
    _rim_z: float | None = None
    _over_rim = False  # the gripper is inside the container's footprint

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
        self._last_box = box
        self._near_rim_v = _near_rim_v(text)
        if box is None:
            self._debug("detect", image=img, label=prompt, note="no container")
            return None
        x, y, w, h = box
        px = (x + w / 2.0, min(float(IMG_H - 1), float(y + h)))
        self._debug("detect", image=img, label=prompt, box_px=list(box), point_px=list(px), near_rim_v=self._near_rim_v)
        return px

    def _rim_height(self, near_x: float) -> float:
        """Rim height above the floor: the near-rim row's ray crossing the
        vertical through the near face, clamped into the workable band.

        The detection's own top edge is the fallback and it reads HIGH — that
        row is the FAR rim seen from above, 15-75 mm over the near one on
        typical boxes — so near_rim_y is used whenever the model returned it."""
        p = self._p
        floor = p["rim_z_min"]
        if self._last_box is None:
            return floor
        x, y, w, _h = self._last_box
        u = min(float(IMG_W - 1), x + w / 2.0)
        v = self._near_rim_v if self._near_rim_v is not None else float(y)
        source = "near_rim" if self._near_rim_v is not None else "bbox_top"
        raw = pixel_to_height(u, v, p["tilt_deg"], near_x)
        rim = floor if raw is None else max(floor, min(p["rim_z_max"], raw))
        self._rim_z = rim
        self.logger.info(f"[DropInBox] rim {rim:.3f} (raw={raw}, from {source}) at near_x={near_x:.3f}")
        self._debug("rim", rim_z=rim, rim_raw=raw, rim_source=source, rim_v=v, near_x=near_x)
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
        """The gripper has something in it. Joints decide; the wrist camera
        only gets a say when they say no, so a mis-read j6 cannot strand a
        genuinely carried object."""
        j6 = self._j6()
        by_joint = j6 is not None and HOLDING_J6[0] < j6 < HOLDING_J6[1]
        seen = self._wrist_sees_object() if not by_joint else None
        held = by_joint or seen is True
        self.logger.info(f"[DropInBox] hold check: j6={j6} joints={by_joint} wrist={seen} -> {held}")
        self._debug("hold", j6=j6, by_joint=by_joint, wrist=seen, held=held)
        return held

    def _release_at(self, near_x: float, near_y: float) -> tuple[float, float, float]:
        """Hover over the container interior and open the claw."""
        p = self._p
        rim = self._rim_height(near_x)
        z = min(p["drop_z_max"], rim + p["release_clear_m"])
        x, y = self.manipulation.clamp_reach(near_x + p["drop_inset"], near_y)
        self._debug("release", target_xyz=[x, y, z], rim_z=rim)

        self.manipulation.torque_on()
        try:
            self.manipulation.move_to(x, y, z, pitch=p["arm_pitch"], duration=p["hover_s"], tolerance_xy=0.06)
        except ArmFailed as e:
            raise SkillFailed(
                f"Could not hold the gripper over the rim at z={z:.2f} m — "
                f"the container looks too tall for this arm ({e})"
            ) from e
        # Latched BEFORE the claw opens: a cancel during the settle below must
        # still lift out of the container, and an unlatched flag folded REST
        # straight through the wall.
        self._over_rim = True
        self.check_cancelled()  # last exit before the object leaves the claw
        self.manipulation.gripper_open(duration=1.0)
        self.sleep(p["release_settle_s"])
        return x, y, z

    def _retract(self) -> None:
        """Best-effort teardown: straight up off the rim, then fold. Never
        raises. Lifting first matters — REST swings the gripper forward and
        down, which would drag it through the container wall."""
        try:
            if self._over_rim:
                self.manipulation.move_by(dz=self._p["lift_after_m"], duration=1.0, tolerance_xy=None, tolerance_z=None)
                # Committed teardown (runs after cancel): time.sleep on purpose.
                time.sleep(0.3)
            self.manipulation.move_joints(self.manipulation.REST, duration=3.0)
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

        self._last_box = None
        self._near_rim_v = None
        self._rim_z = None
        self._over_rim = False
        released_z = None
        try:
            self.head.set_position(int(round(self._p["tilt_deg"])))
            if not self._holding():
                self.fail("I'm not holding anything to put away")

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
