# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Drive up to an open cabinet and photograph the inside of it.

The approach is the shared floor approach pick and drop use: localize the
opening by the point where it meets the floor, then servo the base until that
point sits in the sweet box, which puts the robot in front of the middle of the
opening at a fixed standoff.

Then the arm goes up to shelf height and takes four wrist-camera views — ahead,
left, right, down. They are pushed as skill feedback, which is what puts an
image in front of the agent: the agent reads the newest four event images each
turn, so this skill sends exactly four and attaches no other, and the agent does
the reasoning about what is in there.
"""

import math
from typing import TYPE_CHECKING

from innate_skills.approach import APPROACH_PARAMS, NAV_ARM, Debug, FloorApproach, ask_head

from innate import (
    Head,
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
from innate.exceptions import ArmFailed, ArmUnhealthy
from innate.geometry import IMG_H

if TYPE_CHECKING:
    from innate_skills.approach import Pixel

# joint1 sits 0.053 m right of base_link (URDF), so the arm — and the wrist
# camera on it — hangs off-centre. Parking the OPENING at this y puts the
# camera on its middle, which is what "in front of the cabinet" has to mean
# when the thing doing the looking is the arm.
ARM_Y_OFF = -0.05285

# (label, pan, tilt) in radians, off straight ahead. Pan swings the camera
# about the base axis (+ is left), tilt pitches it down.
VIEWS = (
    ("ahead", 0.0, 0.0),
    ("to the left", 0.40, 0.0),
    ("to the right", -0.40, 0.0),
    ("down at the shelf", 0.0, 0.50),
)

PARAMS = {
    **APPROACH_PARAMS,
    # -20 keeps the 0.45 m park's floor point at v~288, below frame centre with
    # room under it.
    "tilt_deg": -20.0,
    # ZERO holds the fingertips ~0.36 m ahead of base_link; park nearer and the
    # arm unfolds into the cabinet face instead of in front of it.
    "sweet_x": 0.45,
    "box_y": ARM_Y_OFF,
    # A container's detected edge jitters tens of pixels between looks, so the
    # bands are wide: ±30 px of bearing (~0.07 m at the park) for the flow servo
    # and the full ±50 for a Gemini re-read to count as parked. Tighter, the
    # servo limit-cycles on detector noise instead of stopping.
    "box_half_px": 50.0,
    "box_half_v_px": 25.0,
    "accept_frac": 0.6,
    "hold_frac": 1.0,
    # PEEK. Where the wrist camera looks in from, base_link metres — ZERO puts
    # it at about (0.31, 0.21), which is below the shelf. Reach shrinks with
    # height, so a higher look is a shorter one: the arm reaches 0.387 m out at
    # z=0.21 but only 0.289 at z=0.34, and peek_reach must stay under that.
    "peek_reach": 0.24,
    # Radius the arm rises at before it reaches in. Tucked, so the lift is
    # near-vertical: the wrist must stay between 0.077 and 0.326 m of the
    # shoulder (the two links' difference and sum), and 0.12 sits at 0.26.
    "lift_reach": 0.12,
    "peek_z": 0.34,
    "peek_pitch": 0.0,
    "peek_s": 3.0,
    "view_s": 1.5,
    "view_settle_s": 1.0,
    # Room given back once the views are in, so the run does not end with the
    # robot nose-to-nose with an open cabinet for whatever drives next.
    "back_off_m": 0.10,
}


class LookInsideCabinet(Skill):
    """Drive up to an open cabinet, cupboard or under-sink storage space and
    photograph what is inside it (prompt='the open cabinet under the sink').
    The robot parks in front of the middle of the opening, raises the arm to
    shelf height and sends four wrist-camera views — ahead, left, right and
    down — for you to read; it does not judge the contents itself, so look at
    the images and say what is in there. The cabinet must already be open and
    visible: this neither opens doors nor searches other rooms. It ends backed
    off a little from the cabinet with the arm folded back to rest."""

    manipulation: Manipulation
    mobility: Mobility
    head: Head
    main_image: MainImage | None
    odom: Odometry | None
    wrist_image: WristImage | None

    _p = PARAMS
    # Telemetry for the /approach debug page; built per run, shared with the
    # FloorApproach so both halves publish through one node-bound publisher.
    _dbg: Debug | None = None
    _out = False  # the arm is past the cabinet front, so teardown must back out

    @resource
    def _proxy(self):
        return gemlib.make_client()

    def _debug(self, stage: str, *, image: str | None = None, **fields: object) -> None:
        if self._dbg is not None:
            self._dbg(stage, image=image, **fields)

    def _detect_px(self, prompt: str) -> "Pixel | None":
        """Head frame -> where the opening meets the floor, or None.

        The bottom edge, not the centre: a point at shelf height back-projects
        far past the cabinet (the same reason drop_in_box localizes a box by
        its bottom edge)."""
        text, img = ask_head(
            self,
            self._proxy,
            f"Find '{prompt}' in this image — an OPEN cabinet, cupboard or storage space "
            "standing on the floor whose interior is visible through the opening. Return "
            'ONLY a JSON list of matches, each {"box_2d":[ymin,xmin,ymax,xmax]} normalized '
            "0-1000, best first. box_2d is TIGHT around the OPENING — the gap you could "
            "reach into, down to where it meets the floor — not around a door swung out in "
            "front of it. Empty list if no open cabinet is visible.",
            self._p["settle_s"],
        )
        box = vision.parse_det_box(text)
        if box is None:
            self._debug("detect", image=img, note="no match", label=prompt)
            return None
        x, y, w, h = box
        px = (x + w / 2.0, min(float(IMG_H - 1), y + h))
        self._debug("detect", image=img, label=prompt, box_px=[x, y, w, h], point_px=list(px))
        return px

    def _peek(self) -> None:
        """Up first, then in — never both at once.

        One move to the peek pose interpolates diagonally, and so did the ZERO
        this used to stage through: ZERO holds the gripper at (0.31, 0.21),
        further out and lower than the peek, so the arm swept forward at shelf
        height before rising and kicked what was standing there. Lifting tucked
        against the body and only then swinging out keeps the whole path either
        above the shelf or behind the front."""
        self.manipulation.torque_on()
        self._lift()
        self._view(0.0, 0.0)
        self._out = True

    def _lift(self) -> None:
        """Straight up to peek height, held in close to the body."""
        p = self._p
        self.manipulation.move_to(
            p["lift_reach"],
            0.0,
            p["peek_z"],
            pitch=p["peek_pitch"],
            duration=p["peek_s"],
            tolerance_xy=None,
            tolerance_z=None,
        )

    def _view(self, pan: float, tilt: float) -> None:
        """Point the wrist camera `pan` left and `tilt` down from straight
        ahead, at peek height. Raises ArmFailed if the pose is unreachable.

        The pan is a swing about the base axis, written so the request is one
        the arm can meet exactly: five joints leave the gripper's yaw as j1 and
        nothing else, so a pose whose yaw disagrees with its own bearing
        (atan2(y, x)) asks j1 to be in two places, and the IK answers with a
        compromise. Yaw and bearing are the same number here by construction."""
        p = self._p
        r = p["peek_reach"]
        # Unverified on purpose: a missed pose would send move_to into
        # recover(), which reboots the servos torque-off and drops the arm.
        self.manipulation.move_to(
            r * math.cos(pan),
            r * math.sin(pan),
            p["peek_z"],
            pitch=p["peek_pitch"] + tilt,
            yaw=pan,
            duration=p["view_s"],
            tolerance_xy=None,
            tolerance_z=None,
        )

    def _scan(self) -> int:
        """Pan the wrist over VIEWS and send each frame to whoever is running
        this. -> how many views actually produced a frame.

        Every view is an absolute pose, not a nudge off the last one, so a
        refused view costs only itself and nothing compounds."""
        sent = 0
        for label, pan, tilt in VIEWS:
            try:
                self._view(pan, tilt)
            except ArmFailed as e:
                self.logger.warning(f"[LookInsideCabinet] no view {label} ({e})")
                continue
            sent += self._send_view(label)
        return sent

    def _send_view(self, label: str) -> int:
        """One wrist frame to whoever is running this. -> 1 if a frame went."""
        self.sleep(self._p["view_settle_s"])  # the wrist feed lags the servo
        frame = self.wrist_image
        if frame is None:
            self.logger.warning(f"[LookInsideCabinet] view {label} moved but no wrist frame arrived")
            return 0
        self.feedback(f"Inside the cabinet, looking {label}.", frame)
        return 1

    def _back_off(self, approach: FloorApproach) -> None:
        """Tuck the arm in, give the cabinet room, then fold to rest. In that
        order: backing up with the gripper still past the front drags it along
        the shelf, so an arm that will not tuck keeps the base where it is —
        and only once the base has given the cabinet room can REST swing the
        gripper down and forward without sweeping the shelf."""
        try:
            self._lift()
        except ArmFailed as e:
            self.logger.warning(f"[LookInsideCabinet] arm still in the cabinet ({e}); not backing off")
            return
        self._out = False
        approach.drive(-self._p["back_off_m"])
        self._fold()

    def _fold(self) -> None:
        """Best-effort teardown, never raises. An arm that got as far as the
        opening comes back out the way it went in — tuck in at height, then
        fold — because REST from inside the cabinet swings the gripper down and
        forward through the shelf."""
        try:
            if self._out:
                self._lift()
            self.manipulation.rest(duration=3.0)
        except Exception as e:  # noqa: BLE001 — teardown must not mask the run result
            self.logger.warning(f"[LookInsideCabinet] could not fold the arm: {e}")

    def execute(self, prompt: str = "the open cabinet") -> SkillReturn:
        """Drive to `prompt` and photograph the inside of it."""
        if self._proxy is None:
            self.fail("Innate proxy not configured (INNATE_SERVICE_KEY)")

        peeking = False
        self._out = False
        self._dbg = Debug(self, self._p)
        try:
            self.head.set_position(int(round(self._p["tilt_deg"])))
            # Fold clear of the head camera before searching: REST pitches the
            # gripper up over the very floor the cabinet stands on.
            self.manipulation.move_joints(NAV_ARM, duration=3.0)

            approach = FloorApproach(self, self._p, self._detect_px, debug=self._dbg)
            self.say(f"Looking for {prompt}.")
            xy = approach.search(prompt)
            approach.position_above(prompt, xy)

            self.say("Taking a look inside.")
            self._peek()
            peeking = True
            sent = self._scan()
            if not sent:
                self.fail("Parked at the cabinet and raised the arm, but no wrist view came back")
            self._back_off(approach)
            # No evidence image on the result: it would be a fifth event image
            # and push the first view out of the agent's four.
            return (
                f"Looked inside '{prompt}' and sent {sent} wrist views from in there — ahead, "
                f"left, right and down. Read them to say what is in the cabinet. Backed off "
                f"{self._p['back_off_m']:.2f} m afterwards and folded the arm back to rest."
            )
        except ArmFailed as e:
            self.fail(str(e))
        except ArmUnhealthy as e:
            self.say("My arm isn't responding properly, stopping.")
            self.fail(f"Arm servo failure: {e}")
        finally:
            self.mobility.stop()
            # A run that peeked and finished already folded on the way out;
            # one that peeked and then failed stays raised, because the base
            # never backed off and REST from there sweeps the shelf.
            if not peeking:
                self._fold()
            self.head.set_position(0)
