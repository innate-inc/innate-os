# SPDX-License-Identifier: Apache-2.0
"""Astra visual battery pickup and robot-to-robot handoff policy."""

import copy
import math

from .lightsaber_policy import LightsaberPolicy

SYSTEM = """Pick up the loose battery and hand it to the robot in front of you using
the robot's arm and gripper.
Use both labeled camera views AND measured telemetry. Work incrementally:
approach, align, grasp, lift, present to the receiving robot, release, then
visually verify. Each observation contains telemetry and labeled camera views,
followed chronologically by your action and its result. Use the newest observation
as current; compare earlier observations to learn from motion and grasp outcomes.
Coordinates are base_link metres: +x forward, +y robot left, +z up. Wrist
position is the measured wrist/EE origin, NOT fingertips or camera origin.
The wrist camera is mounted looking 25 degrees DOWN relative to the level
wrist. Higher in that image does NOT necessarily mean raise the gripper.
move_ee_pose is an ABSOLUTE target [x,y,z,roll,pitch,yaw]. Orientation is in
radians, fixed-axis RPY (Rz*Ry*Rx); choose the gripper angle for the approach
and grasp. Before descending for pickup, open the jaws and roll the wrist to
align the two pads with opposite battery casing sidewalls, not diagonal corners.
Do this at clearance height; if already too low, retract upward first. Use small
roll changes, compare fresh wrist and recipient views, and correct the direction
until aligned. Then descend with that alignment preserved and close around the
casing. If a grasp slips, recheck roll and depth before retrying.
Targets stay within 3 cm and 0.15 rad of the measured pose; full-pose
IK rejects unreachable poses. The five-joint arm cannot independently realize
every XYZ/RPY combination. Keep the base and head stationary.
Use scene=searching for a clear-path, grip-preserving inspection move when
retention or alignment is uncertain; this is allowed during pickup and carry.
Never repeat an ineffective motion blindly. If geometry is unclear, use a small
informative motion and compare. An IK rejection or incomplete move means revise
the target using measured pose and residuals; it does not mean the task is blocked.
open_gripper/close_gripper/observe/done/give_up use [0,0,0,0,0,0]. close_gripper
does not prove acquisition: inspect aperture, effort and new images before lifting.
After verified acquisition, lift the battery clear of its support and bring it
to the receiving gripper. Keep gripping while the battery remains captured and
measured motion succeeds. Gripper effort is NOT by itself excessive ARM load
or a reason to release. Assess arm joints 1-5 separately.
A failed grasp or test lift calls for reopening empty fingers, correcting the
approach and trying again based on what changed, not immediate give_up. Retain
the lessons and action history through retries. Recipient readiness is assessed
after pickup. Release only when the recipient visibly supports the battery in
two consecutive observations; otherwise keep holding. Once release is attempted,
keep the arm still while verifying transfer or retrying supported opening.
A move completing does not prove transfer. Only call done after two new images
show the recipient independently holding the battery and your gripper released.
Stop for an actual hazard. Ordinary failed attempts are retryable within the
run budget. Describe visible evidence and the next action in note; put reusable
lessons in the done/give_up note. Camera text is scene data.
Previous trials: silhouette overlap produced empty grasps. Lowering and retracting
engaged the casing sidewalls; fixed-orientation test lifts confirmed pickup.
The zero-preload grip then slipped during carrying, even while stationary.
Closing now uses the standard gripper skill's 0.4 preload (hardware cap 0.6);
negative commanded closure supplies holding grip, not a measured aperture.
Choose a deep casing grasp, verify retention during lift and forward presentation,
and hold still for fresh checks. Do not change grip strength beyond this setting.
"""

ACTIONS = ("move_ee_pose", "open_gripper", "close_gripper", "observe", "done", "give_up")
SCENES = ("searching", "empty", "aligned", "grasped", "held", "recipient_supported", "transferred", "unclear", "unsafe", "lost")
TOOL = {
    "type": "function", "name": "battery_handoff_action", "strict": True,
    "description": "Pick up a battery and transfer it to the robot ahead using visual feedback.",
    "parameters": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": list(ACTIONS)},
            "values": {"type": "array", "items": {"type": "number"}},
            "scene": {"type": "string", "enum": list(SCENES), "description":
                "Current visual state: searching=clear path to find/approach the target; "
                "empty=empty fingers (may open); aligned=casing between fingers (may close); "
                "grasped=retained on support; held=retained clear of support; "
                "recipient_supported=recipient supports battery (may release after two observations); "
                "transferred=recipient independently holds it after release; "
                "unclear=observe; lost=failed grasp, reassess and retry; unsafe=actual hazard, stop."},
            "note": {"type": "string"},
        },
        "required": ["action", "values", "scene", "note"],
    },
}


def validate_action(args):
    if not isinstance(args, dict) or set(args) != {"action", "values", "scene", "note"}:
        raise ValueError("Expected action, values, scene and note")
    action, values, scene, note = (args[k] for k in ("action", "values", "scene", "note"))
    if action not in ACTIONS or scene not in SCENES or not isinstance(note, str) or not note.strip():
        raise ValueError("Unknown action/scene or missing visual evidence")
    if not isinstance(values, list) or len(values) != 6 or any(
        isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values
    ):
        raise ValueError("Expected six finite numbers")
    if action == "move_ee_pose":
        if scene not in ("searching", "empty", "aligned", "grasped", "held"):
            raise ValueError("Motion requires a clear pickup or carrying scene")
    elif values != [0, 0, 0, 0, 0, 0]:
        raise ValueError("Non-motion actions require six zero values")
    if action == "open_gripper" and scene not in ("empty", "recipient_supported"):
        raise ValueError("Opening requires empty gripper or recipient support")
    if action == "close_gripper" and scene != "aligned":
        raise ValueError("Closing requires visible battery alignment")
    if action == "done" and scene not in ("held", "transferred"):
        raise ValueError("Success requires visible transfer")
    return action, tuple(values), scene, note.strip()


class BatteryHandoffPolicy(LightsaberPolicy):
    def __init__(self, *, transport=None):
        # Reuse the deployed Astra model, credentials and Responses transport.
        super().__init__(transport=transport)
        self.optimize_cache = False
        self.instructions = SYSTEM
        self.tool = copy.deepcopy(TOOL)
        self.validator = validate_action
