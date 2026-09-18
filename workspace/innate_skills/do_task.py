# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Do what the words say, with the arm and the base, one model decision at a time.

There is no task-specific code here. Each step the model sees the head camera
with a metric floor grid drawn on it, the wrist camera with the fingertip aim
marked, the arm's state and what its earlier actions did, and answers with one
JSON action. The grid is the whole grounding: it turns "the cube is there" into
the base_link metres the arm and the base take directly, so the same loop
drives any model the robot can reach and any task the primitives can express.
"""

import json
import math
from typing import Literal

import cv2
import numpy as np
from innate_llm import Image, Message, Request, Role, Text, Thinking

from innate import (
    Arm,
    Head,
    HeadState,
    Llm,
    MainImage,
    Manipulation,
    Mobility,
    Odometry,
    Skill,
    SkillReturn,
    WristImage,
)
from innate.exceptions import ArmFailed, ArmUnhealthy
from innate.geometry import arm_bearing, floor_to_pixel

HEAD_TILT_DEG = -20.0
GRIP_STRENGTH = 0.6
GRIPPER_EMPTY_J6 = -0.085
# Folded with the wrist flat: REST pitches the gripper up into the head camera.
NAV_ARM = [1.5708, -1.2195, 1.5723, 0.06, -0.47]
# Where a graspable floor object appears in the wrist view (measured in sim: the end-effector lifts a
# 2 cm cube from 2 cm behind it to 4 cm ahead, and over that window the cube stays within ~40 px of
# this pixel from hover height down to the floor).
WRIST_AIM_PX = (320, 280)
# The arm reports its wrist origin; the fingertips land this far ahead of it in the top-down grasp.
FINGERTIP_X_OFF = -0.01
DRIVE_MAX_M, TURN_MAX_DEG, LIFT_Z = 0.6, 90.0, 0.22
# One look per few centimetres: a grid estimate is good to ~5 cm, so a move that commits further than
# this on one estimate lands on or beside the target and the model then reasons from a floor-level view.
NUDGE_MAX_M = 0.05
GRID_X, GRID_Y = np.arange(0.2, 1.01, 0.1), np.arange(-0.4, 0.41, 0.1)
HISTORY = 12
ModelChoice = Literal[
    "default",
    "google:gemini-3.8-flash",
    "google:gemini-3.6-flash",
    "openai:gpt-6-astra",
    "anthropic:claude-fable-5-1",
    "anthropic:claude-opus-5",
    "anthropic:claude-sonnet-5",
    "anthropic:claude-haiku-4-5",
]

_REACH = (
    f"x {Manipulation.REACH_X[0] + FINGERTIP_X_OFF:.2f}-{Manipulation.REACH_X[1] + FINGERTIP_X_OFF:.2f}, "
    f"y {Manipulation.REACH_Y[0]:.2f}..{Manipulation.REACH_Y[1]:.2f}"
)
SYSTEM = f"""You control MARS, a small mobile robot with a 5-joint arm and a two-finger gripper, to carry out a task
given in words. Each turn you get: the head camera with a floor grid in base_link metres (+x forward, +y left,
labels are metres; the orange box is the arm's reach), the wrist camera (the crosshair is where the fingertips
land), the arm's state, and what your earlier actions did. Reply with ONE JSON object and nothing else:
{{"see": "<one sentence: what you observe that matters>", "do": "<action>", ...its parameters}}.

Actions:
- drive: forward_m (-0.6..0.6, negative backs up), turn_deg (-90..90, positive turns left). Turns, then drives,
  with the arm wherever it is (refused while the fingertips are on the floor).
- nudge: dx, dy, dz — move the FINGERTIPS by that much (metres, base_link axes, each within
  +-{NUDGE_MAX_M * 100:.0f} cm) from where the status says they are. pitch_deg (0 = gripper points straight
  ahead, 90 = straight down) and roll_deg (0 = fingers straddle along y, 90 = along x) are optional and keep
  their last value. z 0.03 is the floor, 0.22 is clear for carrying. The first nudge from a fold only brings
  the arm out to its zero pose (straight ahead, horizontal).
- grip: close (true/false). Closing reports whether the fingers stopped on something; a gripper that closed on
  nothing stays closed until you open it.
- rest: fold the arm away (keeps whatever it holds).
- done: message — the task is complete. fail: message — it cannot be done.

Facts about this body:
- The arm only reaches the box {_REACH}; a target outside it is refused, and the base has to move instead.
- The head camera cannot see the floor closer than the bottom of the grid, and the arm can hide part of its view
  (folded, the lower right; rest folds it away). Something that vanished after driving forward is usually right in front of the wheels: back up a
  little rather than search. Grid distances tend to read long; drive a little less than the difference.
- The head view is for reading positions off the grid; the wrist view is for the last few centimetres: at any
  height, what sits under its crosshair is what the fingers will close on (give or take 2 cm).
- The fingers close where they are: they only catch what is between them at that height, so something on the
  floor is grasped with the fingertips at z 0.03, not from above it.
- A descent stops when the fingers touch something, so settling above the asked z means contact, not a limit.
- Fingers closed on an edge let go on the way up; check the wrist view after lifting.
Every step costs seconds, so prefer one decisive move over many small ones, and judge each action by the new
images rather than by the plan. If an action changed nothing, do something different rather than repeating it."""


class DoTask(Skill):
    """Carry out a task described in words — anything that can be done by
    driving the base, moving the arm and gripping, such as picking an object
    off the floor, pushing it somewhere or dropping it in a container. The
    robot's vision model plans every step from the cameras, so it is slower
    and less reliable than a dedicated skill; use it when no dedicated skill
    fits. `task` is the instruction; `model` runs it on a model other than
    the robot's default."""

    main_image: MainImage
    wrist_image: WristImage
    arm: Arm
    odom: Odometry
    head_position: HeadState
    manipulation: Manipulation
    mobility: Mobility
    head: Head
    llm: Llm

    def execute(self, task: str, max_steps: int = 30, model: ModelChoice = "default") -> SkillReturn:
        llm = self.llm if model == "default" else Llm(model)
        self._rpy = (0.0, 0.0, 0.0)
        # The last run may have ended holding or closed; a held object reads ~0.5, open ~0.85.
        self._closed = (self.arm.gripper or 0.0) < Manipulation.GRIPPER_OPEN - 0.15
        history: list[str] = []
        self.overlay.begin(task)
        self.head.set_position(int(HEAD_TILT_DEG))
        try:
            for step in range(1, max_steps + 1):
                self.sleep(0.4)  # the cameras catch up with the last motion
                decision = self._decide(llm, task, history, step, max_steps)
                see, action = str(decision.get("see", "")), str(decision.get("do", ""))
                self.overlay.readout(see)
                if action == "done":
                    return str(decision.get("message") or "Done.")
                if action == "fail":
                    self.fail(str(decision.get("message") or "The model gave up."))
                self.feedback(f"saw: {see} | did: {_describe(decision)}")
                result = self._act(decision)
                self.feedback(f"result: {result}")
                history.append(f"{step}. saw: {see} | did: {_describe(decision)} | result: {result}")
            self.fail(f"Out of steps ({max_steps}) before finishing: {task}")
        finally:
            self.mobility.stop()  # the arm stays where the model left it: resting it is its call

    # --- one decision ---

    def _decide(self, llm: Llm, task: str, history: list[str], step: int, max_steps: int) -> dict:
        # A folded arm's wrist camera stares at the floor by the robot's flank, and what it shows there
        # has tempted every model into reaching for it; it comes back once the arm is out.
        folded = self.arm.x < 0.15
        views = "Image 1 is the head camera with the floor grid" + (
            "; the arm is folded, so there is no wrist view." if folded else ", image 2 the wrist camera."
        )
        text = (
            f"Task: {task}\nStep {step} of {max_steps}.\n{self._status()}\n"
            f"So far:\n{chr(10).join(history[-HISTORY:]) or '(nothing yet)'}\n{views} Reply with the JSON action."
        )
        images = (Image(self._head_view()),) if folded else (Image(self._head_view()), Image(self._wrist_view()))
        message = Message(Role.USER, (Text(text), *images))
        request = Request(system=SYSTEM, messages=(message,), thinking=Thinking.LOW, temperature=0.0)
        reply = llm.run(request, logger=self.logger)
        if reply is None:
            self.fail(f"{llm.model} is unreachable")
        self.logger.info(f"[do_task] {llm.model}: {reply.message.text()}")
        return _parse_json(reply.message.text())

    def _status(self) -> str:
        arm = self.arm
        return (
            f"Fingertips at x={arm.x + FINGERTIP_X_OFF:.2f} y={arm.y:.2f} z={arm.z:.2f} m, "
            f"pitch {math.degrees(self._rpy[1]):.0f} deg, roll {math.degrees(self._rpy[0]):+.0f} deg. "
            f"Gripper {self._grip_state()}. Head tilt {self.head_position.pitch_degrees:.0f} deg."
        )

    def _grip_state(self) -> str:
        """Fingers that stop short of the closed stop are holding something; the servo reads the stop itself
        (about GRIPPER_EMPTY_J6) only when it closed on air."""
        j6 = self.arm.gripper
        if j6 is None:
            return "unknown"
        if not self._closed:
            return "open"
        return (
            "closed, empty"
            if j6 < GRIPPER_EMPTY_J6 + 0.04
            else f"closed, holding something (fingers stopped at {j6:.2f})"
        )

    def _act(self, decision: dict) -> str:
        do = {"drive": self._drive, "nudge": self._nudge, "grip": self._grip, "rest": self._rest}
        run = do.get(str(decision.get("do")))
        if run is None:
            return "not a known action; reply with one JSON object whose 'do' is one of the listed actions"
        try:
            return run(decision)
        except (ArmFailed, ArmUnhealthy) as e:
            return f"arm error: {e}"

    # --- actions ---

    def _drive(self, act: dict) -> str:
        turn = _clamp(_num(act, "turn_deg"), TURN_MAX_DEG)
        forward = _clamp(_num(act, "forward_m"), DRIVE_MAX_M)
        if 0.15 <= self.arm.x and self.arm.z < 0.05:
            return "nothing moved: the fingertips are on the floor and would drag; lift them before driving"
        turned = abs(turn) < 1.0 or self.mobility.rotate_by(self._xyt, math.radians(turn), logger=self.logger)
        driven = abs(forward) < 0.01 or self.mobility.drive(self._xyt, forward, logger=self.logger)
        return f"turned {turn:+.0f} deg, drove {forward:+.2f} m" + ("" if turned and driven else " (stopped short)")

    def _nudge(self, act: dict) -> str:
        if self.arm.x < 0.15:
            self.manipulation.move_joints(Manipulation.ZERO[:5], duration=2.0)
            self._rpy = (0.0, 0.0, 0.0)
            return "arm brought out to its zero pose; " + self._arm_report(self.arm.z)
        cur = self.arm
        dx, dy, dz = (_clamp(_num(act, k), NUDGE_MAX_M) for k in ("dx", "dy", "dz"))
        x, y, z = cur.x + dx, cur.y + dy, cur.z + dz
        if (why := _out_of_reach(x, y)) is not None:
            return why
        roll = math.radians(_num(act, "roll_deg", math.degrees(self._rpy[0])))
        pitch = math.radians(_num(act, "pitch_deg", math.degrees(self._rpy[1])))
        self._rpy = self._orientation(x, y, roll, pitch)
        self._move_to(x, y, z, duration=0.8)
        return self._arm_report(z)

    def _grip(self, act: dict) -> str:
        if act.get("close"):
            if self.arm.z < 0.05:  # un-press from the floor so the fingers close around the object, not drag it
                self._move_to(self.arm.x, self.arm.y, self.arm.z + 0.01, duration=0.5)
            self.manipulation.gripper_close(GRIP_STRENGTH, duration=1.0)
        else:
            self.manipulation.gripper_open(duration=0.8)
        self._closed = bool(act.get("close"))
        self.sleep(0.8)
        return f"gripper {self._grip_state()}"

    def _rest(self, act: dict) -> str:
        self._fold()
        return "arm folded"

    # --- arm helpers ---

    def _fold(self) -> None:
        if self.arm.x < 0.15:
            return
        if self.arm.z < LIFT_Z - 0.05:
            self._move_to(self.arm.x, self.arm.y, LIFT_Z, duration=1.0)
        self.manipulation.move_joints(NAV_ARM, duration=2.0)

    def _move_to(self, x: float, y: float, z: float, duration: float = 1.5) -> None:
        roll, pitch, yaw = self._rpy
        self.manipulation.move_to(
            x, y, z, roll=roll, pitch=pitch, yaw=yaw, duration=duration, tolerance_xy=None, tolerance_z=None
        )

    def _orientation(self, x: float, y: float, roll: float, pitch: float) -> tuple[float, float, float]:
        """The tool lies in the arm's own plane, so its yaw is the arm's bearing to the target; near vertical
        RPY is gimbal-locked and only that yaw keeps the base rotation out of the wrist roll."""
        return roll, pitch, arm_bearing(x, y)

    def _arm_report(self, wanted_z: float) -> str:
        self.sleep(0.2)
        arm = self.arm
        report = f"fingertips at x={arm.x + FINGERTIP_X_OFF:.2f} y={arm.y:.2f} z={arm.z:.2f}"
        if arm.z - wanted_z > 0.03:
            report += f" — the descent stopped {arm.z - wanted_z:.2f} m above the asked z={wanted_z:.2f}"
        return report

    def _xyt(self) -> tuple[float, float, float] | None:
        return Mobility.odom_xyt(self.odom)

    # --- what the model sees ---

    def _head_view(self) -> bytes:
        img = _decode(self.main_image.jpeg)
        tilt = self.head_position.pitch_degrees
        for x in GRID_X:
            _polyline(img, [floor_to_pixel(x, y, tilt) for y in np.arange(-0.5, 0.51, 0.05)], (200, 200, 200))
            for y in (-0.45, 0.45):
                _label(img, f"x{x:.1f}", floor_to_pixel(x, y, tilt))
        for y in GRID_Y:
            _polyline(img, [floor_to_pixel(x, y, tilt) for x in np.arange(0.2, 1.01, 0.05)], (200, 200, 200))
            _label(img, f"y{y:+.1f}", floor_to_pixel(0.22, y, tilt))
        (x0, x1), (y0, y1) = Manipulation.REACH_X, Manipulation.REACH_Y
        x0, x1 = x0 + FINGERTIP_X_OFF, x1 + FINGERTIP_X_OFF
        box = [(x0, y0), (x0, y1), (x1, y1), (x1, y0), (x0, y0)]
        _polyline(img, [floor_to_pixel(x, y, tilt) for x, y in box], (0, 140, 255), 2)
        return _encode(img)

    def _wrist_view(self) -> bytes:
        img = _decode(self.wrist_image.jpeg)
        u, v = WRIST_AIM_PX
        cv2.line(img, (u - 25, v), (u + 25, v), (0, 255, 255), 2)
        cv2.line(img, (u, v - 25), (u, v + 25), (0, 255, 255), 2)
        _label(img, "fingertips", (u + 8, v - 8))
        return _encode(img)


def _describe(decision: dict) -> str:
    return json.dumps({k: v for k, v in decision.items() if k != "see"}, separators=(",", ":"))


def _parse_json(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _out_of_reach(x: float, y: float) -> str | None:
    """Past the box the arm still moves but cannot reach the floor, so a target there is refused with the fix."""
    (x0, x1), (y0, y1) = Manipulation.REACH_X, Manipulation.REACH_Y
    if x0 - 0.01 <= x <= x1 + 0.01 and y0 - 0.01 <= y <= y1 + 0.01:
        return None
    x0, x1, x = x0 + FINGERTIP_X_OFF, x1 + FINGERTIP_X_OFF, x + FINGERTIP_X_OFF
    return (
        f"nothing moved: ({x:.2f}, {y:.2f}) is outside the arm's reach box (x {x0:.2f}-{x1:.2f}, y {y0:.2f}..{y1:.2f}); "
        "drive the base so the target lands inside the box, then try again"
    )


def _num(act: dict, key: str, default: float = 0.0) -> float:
    value = act.get(key, default)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


def _decode(jpeg: bytes) -> np.ndarray:
    return cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)


def _encode(img: np.ndarray) -> bytes:
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()


def _polyline(img: np.ndarray, points: list, color: tuple, thickness: int = 1) -> None:
    run: list[tuple[int, int]] = []
    for p in points + [None]:
        if p is not None:
            run.append((int(round(p[0])), int(round(p[1]))))
        elif len(run) > 1:
            cv2.polylines(img, [np.array(run, np.int32)], False, color, thickness, cv2.LINE_AA)
            run = []
        else:
            run = []


def _label(img: np.ndarray, text: str, px: tuple | None) -> None:
    if px is None:
        return
    at = (int(round(px[0])), int(round(px[1])))
    if not (0 <= at[0] < img.shape[1] and 0 <= at[1] < img.shape[0]):
        return
    cv2.putText(img, text, at, cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(img, text, at, cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA)
