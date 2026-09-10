# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Slash-and-pick, run twice: identical mechanics, one told the task and one not.

The pair is an ablation on the demonstration itself — whether a recorded episode
is enough for the model to infer a multi-stage task, or whether it has to be
told. Only INSTRUCTIONS differs between the two skills, so a difference in
outcome is the prompt and nothing else. The untold variant therefore leaks
nothing: it sends no object description in its observations, and no object
vocabulary in the JSON schema.

Unlike a pickup skill this one deliberately RELEASES: the gripper opens to drop
one object and again to place another. A grip latch replaces the never-open rule
a carry skill relies on, enforcing only the ordering — no grasp with a full
gripper, no release with an empty one, no finishing while still carrying.
"""

import base64
import hashlib
import json
import math
import os
import time
import uuid
from pathlib import Path
from typing import Literal

from innate_skills.demonstration_skill import LiveGestureObservation, _DemonstrationSkill

from innate import SkillOutput
from innate.demo_actions import (
    ACTION_HELP,
    FIXED_ACTIONS,
    MOTION_ACTIONS,
    check_reachable_move,
    ee_target,
    event_frames,
    joint_target,
    next_holding,
    parse_actions,
    stuck_hint,
    with_limits,
)
from innate.demo_policy import DemonstrationPolicy
from innate.gesture import Gesture

# A demonstration recorded on this robot. Any finalized episode works; see
# docs/skills/gesture-imitation.md for what the loader requires.
DEMONSTRATION = ""

REACH_WITH_BASE = """Reach costs load. The shoulder and elbow servos heat up holding an extended pose, and a sustained
over-reach trips a hardware overload that stops the run. Never solve distance with the arm. Before
you close the gripper on anything, drive the base with base_step until the grasp can happen with
the arm comfortably folded rather than stretched out; judge it from the head view, where a nearly
straight arm means you are too far away. The same applies when you set something down. Repeated
IK rejections, or moves that complete but miss their target, mean you are working at the edge of
reach: close the distance rather than retrying the pose from where you stand."""

WRIST_AIM = """The wrist angle is a target you choose, not one you inherit. A Cartesian target carries three
rotation components as well as three positions, and zeros in the rotations mean keep the angle I
already have, so a run that always sends zeros drags one fixed wrist angle through every phase.
Before you ask where the fingers should be, look at the wrist view and ask where they point: pitch
decides whether they come down onto an object or into it, roll decides which way they straddle it.
Correct the aim in the same step as the approach rather than after it. Five joints cannot reach
every orientation, so a rotation may be met only in part, and a small position miss alongside a
requested rotation is ordinary rather than a failure — read measured_pose and the reported errors
to see what you actually got, then carry on from there."""

SHOULDER_LOAD = """Joint 2 is the shoulder. It carries the weight of the whole arm plus whatever you hold, and unlike
the other joints it has no current limiter protecting it — it simply trips a hardware overload,
goes limp, and the run is over. What trips it is time under load, not a brief reach: passing
through an extended pose is cheap, holding one is not. Every decision you make costs seconds of
real time, so whatever pose you leave the arm in is a pose the shoulder holds while you think.
Therefore: cross the loaded part of a motion in a few decisive steps rather than many small ones,
and never park extended. Before you observe, verify, or reason about what to do next, bring the arm
back toward folded first — a look from a folded pose costs nothing, a look at full extension costs
shoulder current for the whole turn. If a phase needs the arm out there, go in, act, and come back.

Setbacks are not reasons to stop. A move that lands short, an arm that sags below the pose you
asked for, a grasp that closes on nothing, a target IK refuses — all of these are normal and all of
them are information. The measured state you get back is the truth; plan the next move from it
instead of from what you intended. Repetition is the only real failure mode: if the same approach
fails twice, change the approach — a different joint, a different height, a different direction,
come at it from somewhere else. Sag in particular means the arm settled below its target under
load, so aim higher than you want to end up and let it settle into place. Only call stop when the
scene makes the task impossible, never because progress has been difficult.

You have a number for this. Every observation carries shoulder_limit_rad, the qpos[1] to stay
under, alongside shoulder_demo_max, the highest the recorded robot ever needed. The limit is
already far more generous than the demonstration, so being anywhere near it means you are reaching
much further than this task requires. {close_distance}
Judge it by duration, not by the instant. Crossing the limit briefly while acting is fine — what
hurts the joint is sitting there. Do not interrupt an approach, a descent or a grasp to correct a
small excursion: finish the action, then come back down. Retract when you are well over the limit,
or when you are about to hold still above it, and never spend consecutive turns nudging the
shoulder up and down instead of making progress on the task.

The same joint cannot fold backwards indefinitely: past joint2_min_rad, which rides in every
observation and tightens as joint1 nears centre, the arm would swing into the robot's own body.
The driver clamps a command that asks for more without saying so, and the motion then lands short
of what you asked, so a joint step or a Cartesian pose whose solution needs it is refused before
anything moves. Keep the shoulder upright or forward: gain height with joints 3 and 4, or step the
base, rather than folding it further back."""

REACH_NO_BASE = """Reach costs load. The shoulder and elbow servos heat up holding an extended pose, and a sustained
over-reach trips a hardware overload that stops the run. This run cannot drive the base, so you
cannot close distance: work only within comfortable reach, judged from the head view where a
nearly straight arm means you are already too far. Repeated IK rejections, or moves that complete
but miss their target, mean the target is out of reach from here — say so and stop rather than
stretching for it."""

GENERAL = """You are this MARS robot. A recorded demonstration of ONE task is your only description
of what to do: infer the task from it and reproduce it on the live scene. Nobody will tell you
what the task is. Do not guess from object names — there are none — infer from what the recorded
robot actually did across the episode.

Read the demonstration as evidence, not as a script to replay. Its frames carry both camera views
with the measured arm state at that instant: ee_pose (base_link metres, xyz + quaternion), qpos,
gripper_target_rad, head_degrees. Watch gripper_target_rad across the episode: where it changes,
the recorded robot grasped or released something. Those transitions are landmarks, not the task —
most of the work happens between them, and an interval with no gripper change can still contain a
deliberate act on an object. Judge from the arm's motion and from what changes in the scene, not
from the gripper channel alone.
camera_observations gives each image its true source timestamp and nearest arm sample; row_offset_s
exposes frames that are stale by up to a couple of seconds. Do not invent motion between frames —
inspect_demo any source index to look closer.
If frames carry base_command and base_dead_reckoned, the base drove during that recording, so part
of the object's apparent motion is the base and not the arm. Those are integrated from the recorded
commands, not measured odometry. Read the arm's contribution relative to the object.

The live scene is not the recorded one: objects sit differently. Reproduce the recorded INTENT
against what you see now, never recorded joint values as absolute targets. Compare the newest live
pair of images against the previous one to judge whether your last action did what you expected.

Coordinates are base_link metres: +x forward, +y robot left, +z up. Wrist position is the measured
wrist origin, NOT fingertips or camera origin. The wrist camera looks 25 degrees DOWN relative to
the level wrist, so higher in that image does not mean raise the gripper.

Every turn, live carries the robot's measured state, and every target you choose must be built from
those numbers rather than from recorded ones. pose is the end effector as [x,y,z,roll,pitch,yaw],
metres and radians. Note the mismatch with the demonstration: a recorded ee_pose is [x,y,z] plus a
four-component quaternion, so positions compare directly but orientations never do — never copy
recorded orientation components into a target. qpos is the six measured joints in joint1..joint6
order, so joint_step index N moves qpos[N-1], and qpos[5] is the gripper, repeated as gripper.
base is the measured [x,y,yaw] of the base, head_degrees the head tilt, and holding what the grip
latch believes you are carrying.

Choose ONE tool per response. act returns an actions array: {batching} Each decision has exactly
action, pose, reason. The actions available to you on this run are:
{actions}
reason is one short sentence: the observed evidence and the purpose of this action, including,
for a gripper action, what you believe you are grasping or releasing and why now.

Motion never changes the gripper. Every action above that moves the arm or the base carries the
jaws exactly as they are; only open_gripper and close_gripper change them. So a move can never
drop what you are carrying, and a grasp is never a side effect of travelling somewhere.

Grasp from above, or failing that from the front — never from behind. Centring and descending are
two separate moves and must never be combined: first bring the gripper over the object at a
clearance height and settle the roll so the fingers straddle its narrowest dimension, confirm from
the wrist view that the object sits between them, and only then descend straight down and close. A
target that moves sideways while it drops is refused, because sliding across an object at low
height tips it over before the fingers ever close. If the descent ends up off-centre, rise back to
clearance height before correcting — never shuffle sideways down at the object. Coming in from the side shoves an unsecured object away before the fingers meet,
and a low approach catches on whatever it is resting on. Lift straight up before carrying it
anywhere.
Reaching past an object and folding the wrist back to catch it from the far side is the worst case:
joint 4 ends up doubled under, the fingers close toward the robot so the object is pushed away
rather than trapped, and the shoulder is carrying that reach the whole time. If the gripper is
beyond the object, retract until it is in front of the object and approach again — do not bend the
wrist to compensate for having gone too far.

Every observation carries demo_joint_range: the span of each of the five arm joints across the
whole recording. That is the envelope the task actually needs. A joint outside it means your
geometry has diverged from anything the demonstration showed, and the answer is to come back inside
it, not to push further out.

{aim}{reach}

You begin with something already in the gripper. The latch enforces the ordering: you may only
open while holding, and only close while empty. A release is deliberate and irreversible — the
item stays where it falls, so never release while carrying something to a destination you have
not reached yet.
History.execution is the measured physical result, not a prediction. unreachable, not_reached or
rejected means the move did NOT happen: use measured_pose and fresh images to choose a different
approach rather than repeating the target. A completed move does not prove the task progressed.

You are shown the episode in fixed looks, then you record_phases once: the ordered phases you infer,
each with a frame interval, a reference frame and a visually observable advance_when. After that
every response is act.

While you act, the recorded frames you are shown are the phase you are in, sampled across its own
interval, plus the reference frame of the phase before and after. They show how the recorded robot
performed this phase, not merely that it happened — read the progression across them, and when your
phase advances the window moves with it.

A phase is one intent — approach something, act on it, carry it, put it down, withdraw — not one
gripper interval. Do not emit one phase per grasp and release: the stretch before a release often
contains a whole action of its own, and collapsing it loses the step the demonstration was showing.
If the survey shows the arm doing something to an object and the scene changing as a result, that
is its own phase even though the gripper never moved. Name what was done, not merely what was held.
Check your phase list against the survey before you commit it: every visible change in the scene
across the episode should fall inside a phase that describes it.

act carries the phase you are working in. current_phase in the input is where the run believes you
are; phase in your act is where you say you are. You may keep the current phase or move to the next
one, never further, and moving requires previous_phase_complete true with evidence naming what you
saw that satisfies the previous phase's advance_when. Nothing advances on its own — a phase you
have finished stays current until you move it, and you will keep being told you are in it. When the
advance_when you wrote is visibly satisfied, move on in that same act rather than repeating work.
evidence is the live justification for the phase you chose.

Judge a grasp by the aperture, not only by the picture. Every observation carries the live gripper
reading plus the widest and narrowest the demonstration ever reached, which is your scale. Closing
on nothing runs the jaws to their stop, so a live reading at or below the demonstrated narrow end
means you are probably holding nothing: open, reposition and close again. A reading that settles
clearly above it means something is between the fingers, and how far above tells you its width.
This works when the cameras cannot: prefer it over guessing from a picture you cannot read.

A camera that is saturated, blurred, or filled edge to edge by one surface is too close to be
evidence, and looking again from the same pose returns the same non-answer. Retract to a clearance
height and judge from the head view instead. If two fresh looks in a row tell you nothing new,
change the pose that produced them rather than repeating the observation.

Return done only in the final phase, with fresh evidence, and only when your gripper is empty. Two
separate confirming observations close the run. An uncertain result calls for another observation,
not a claim of success. People in view are normal context, not an obstruction; stop only for a
person actually in the path of the motion you are about to make.
"""

TASK = """
THE TASK, stated explicitly: you are holding a plastic toy lightsaber. Slash the prop cactus with
it so the cactus is knocked off its box. Then release the lightsaber and set it aside. Then pick
up the fallen cactus and place it back on top of the box it came from, and release it there.
Four stages: slash, drop the saber, pick up the cactus, put the cactus back on the box.
"""


def build_instructions(allowed, told, chunk_size):
    """The prompt for one run. Only the actions this run permits are described:
    describing a tool the model cannot call is noise it has to resolve, and it
    would keep proposing the missing one and collecting rejections."""
    vocabulary = "\n".join(ACTION_HELP[name] for name in [*MOTION_ACTIONS, *FIXED_ACTIONS] if name in allowed)
    batching = (
        "up to chunk_size joint_step decisions, or exactly one decision of any other kind. Once a "
        "direction is working, return a FULL chunk rather than one step and another look: each "
        "extra call is seconds of latency the shoulder spends holding its pose, and the host still "
        "re-checks telemetry between every step and throws the rest away if anything drifts. Return "
        "a single step only when its outcome genuinely determines what you do next."
        if "joint_step" in allowed and chunk_size > 1
        else "exactly one decision."
    )
    aim = WRIST_AIM + "\n\n" if {"ee_absolute", "ee_delta"} & set(allowed) else ""
    text = (
        GENERAL.replace("{actions}", vocabulary)
        .replace("{batching}", batching)
        .replace("{aim}", aim)
        .replace(
            "{reach}",
            (REACH_WITH_BASE if "base_step" in allowed else REACH_NO_BASE)
            + "\n\n"
            + SHOULDER_LOAD.replace(
                "{close_distance}",
                "Retract with joints 3 and 4 and close the distance with base_step instead."
                if "base_step" in allowed
                else "Retract and work from where the arm is comfortable, or stop and say the "
                "object is out of reach from here.",
            ),
        )
    )
    return with_limits(text + (TASK if told else ""))


class _SlashAndPickPolicy(DemonstrationPolicy):
    """The run's planner with this skill's action vocabulary."""


class _SlashAndPickCactus(_DemonstrationSkill):
    """Shared implementation for both variants. Underscore-prefixed so the skill
    registry treats it as a helper base rather than a third skill."""

    instructions = GENERAL
    # A task-shaped description of the scene. Empty for the untold variant: this
    # field reaches the model in every observation, so it is the main leak path.
    object_description = ""
    decision_timeout = 175
    grip_strength = 0.4
    overview_frames = 24
    shoulder_margin = 0.2  # rad of headroom over what the demonstration needed
    shoulder_hard_max = 0.5  # rad; the run that tripped the shoulder reached 1.04
    frame_selection = "both"
    survey = None
    allowed = (*MOTION_ACTIONS, *FIXED_ACTIONS)
    max_steps = 110
    time_budget_s = 1200

    def make_policy(self, demo):
        policy = _SlashAndPickPolicy(demo, self.chunk_size)
        policy.allowed = self.allowed
        policy.instructions = build_instructions(self.allowed, bool(self.object_description), self.chunk_size)
        # record_phases refuses to run before a detailed look; the forced looks
        # the skill hands it ARE that look.
        policy.inspected_detail = True
        if self.survey is not None:
            policy.add_look("uniform survey of the whole episode", self.survey.frames)
        return policy

    def _open(self, monitor, xml):
        self.manipulation.gripper_open(duration=0.8, block=False)
        self._wait_motion(monitor, xml)
        return {"status": "released", "reason": "Gripper opened; the item stays where it fell"}

    def _close(self, monitor, xml):
        self.manipulation.gripper_close(strength=self.grip_strength, duration=0.8, block=False)
        self._wait_motion(monitor, xml)
        measured = self._observe(monitor, time.monotonic() - 0.2, xml)
        return {
            "status": "closed",
            "reason": "Gripper closed; confirm acquisition from aperture and fresh images before lifting",
            "gripper": measured["gripper"],
            "measured_pose": measured["pose"],
        }

    def _run(self, demonstration, chunk_size, overview_frames, frame_selection, motions):
        from ament_index_python.packages import get_package_share_directory

        if type(chunk_size) is not int or not 1 <= chunk_size <= 10:
            self.fail("chunk_size must be an integer from 1 to 10")
        self.chunk_size = chunk_size
        if type(overview_frames) is not int or not 6 <= overview_frames <= 48:
            self.fail("overview_frames must be an integer from 6 to 48")
        if frame_selection not in ("both", "keyframes", "uniform"):
            self.fail("frame_selection must be both, keyframes or uniform")
        try:
            self.allowed = parse_actions(**motions)
        except ValueError as exc:
            self.fail(str(exc))
        self.frame_selection = frame_selection
        demo = Gesture(
            demonstration,
            image_time_reference=True,
            max_frames=overview_frames,
            uniform=frame_selection == "uniform",
        )
        # "both" is two looks: the evenly binned survey, then the gripper
        # transitions sampled closely. Loading twice costs about half a second.
        self.survey = None
        if frame_selection == "both":
            self.survey = Gesture(demonstration, image_time_reference=True, max_frames=overview_frames, uniform=True)
            close = event_frames(self.survey.grip_events, len(self.survey.poses))
            if close:
                demo = Gesture(demonstration, image_time_reference=True, frame_indices=close)
        # The highest shoulder angle the recording ever needed, over every frame the
        # model is shown. The demonstration finishing below it is the proof that a
        # higher angle means reaching rather than a harder task.
        shown = [*demo.frames, *(self.survey.frames if self.survey else [])]
        shoulder_ceiling = round(max(f["qpos"][1] for f in shown), 3)
        # The demonstration's own maximum leaves no room for a scene that differs
        # from the recorded one, and policing it exactly cost more turns in
        # corrections than it saved in load. The margin is well under the angle
        # that has actually tripped the shoulder.
        shoulder_limit = round(min(shoulder_ceiling + self.shoulder_margin, self.shoulder_hard_max), 2)
        joint_envelope = [
            [round(min(f["qpos"][j] for f in shown), 2), round(max(f["qpos"][j] for f in shown), 2)] for j in range(5)
        ]
        xml = (Path(get_package_share_directory("mars_sim")) / "urdf/mars.urdf").read_text()
        if hashlib.sha256(xml.encode()).hexdigest() != demo.model_hash:
            self.fail("Demonstration robot model differs from the running robot")
        policy = self.make_policy(demo)
        root = Path(os.environ.get("INNATE_OS_ROOT", Path(__file__).resolve().parents[2]))
        run = root / "workspace/custom_skills/.gesture_runs" / ("slashpick-" + uuid.uuid4().hex)
        run.mkdir(parents=True)
        (run / "manifest.json").write_text(
            json.dumps(
                {
                    "skill": self.name,
                    "demonstration": str(demo.path),
                    "model": "gpt-6-astra",
                    "model_sha256": demo.model_hash,
                    "chunk_size": chunk_size,
                    "told_the_task": bool(self.object_description),
                    "frame_selection": frame_selection,
                    "overview_frames": overview_frames,
                    "motion_actions": list(self.allowed),
                }
            )
        )
        icl = self.make_trace(run)
        icl.begin(
            "gpt-6-astra",
            str(demo.path),
            len(demo.poses),
            chunk_size=chunk_size,
            told_the_task=bool(self.object_description),
            frame_selection=frame_selection,
            motion_actions=list(self.allowed),
            overview=[f["index"] for f in demo.frames],
        )
        policy.trace = icl

        self._base_origin = None
        monitor = None
        succeeded = False
        ending = "Run ended without a result"
        holding = True  # every run of this pair starts with the prop in the gripper
        history, rejected, pending = [], set(), []
        expected_qpos = None
        verified = failures = 0
        previous_speed = self.manipulation.safety.max_ee_speed
        try:
            self.manipulation.safety.max_ee_speed = min(previous_speed or 0.03, 0.03)
            self.mobility.stop()
            # The prop was put in the claw by hand, so the standing grip target is
            # whatever the last run left — re-close on it, or every motion carries
            # that stale j6 and the first one lets go of the prop.
            self.manipulation.gripper_close(strength=self.grip_strength, duration=0.8)
            monitor = LiveGestureObservation()
            started = after = time.monotonic()
            for step in range(self.max_steps):
                if time.monotonic() - started > self.time_budget_s:
                    self.fail("Slash-and-pick exceeded its time budget")
                observation = self._observe(monitor, after, xml)
                observation.update(
                    joint2_min_rad=self.manipulation.joint2_floor(observation["qpos"][0]),
                    head_degrees=self.head_position.pitch_degrees,
                    holding=holding,
                    finishing=verified > 0,
                    gripper_closed_rad=demo.grip_range[0],
                    gripper_open_rad=demo.grip_range[1],
                    shoulder_demo_max=shoulder_ceiling,
                    shoulder_limit_rad=shoulder_limit,
                    demo_joint_range=joint_envelope,
                )
                # Only the told variant names the scene; the untold one must not.
                if self.object_description:
                    observation["object"] = self.object_description
                for name, image in observation["images"].items():
                    (run / f"{step:03d}_{name}.jpg").write_bytes(base64.b64decode(image))

                new_batch = not pending
                latency = 0.0
                if new_batch:
                    asked = time.monotonic()
                    values = self._decide(policy, observation, history)
                    latency = time.monotonic() - asked
                    pending = list(values)
                self.check_cancelled()
                current = self._observe(monitor, time.monotonic() - 0.2, xml)
                if math.dist(current["pose"][:3], observation["pose"][:3]) > 0.01:
                    pending = []
                    history.append(
                        {
                            "step": step,
                            "execution": {
                                "status": "stale_observation",
                                "reason": "Arm shifted while planning; discarded the action",
                                "measured_pose": current["pose"],
                            },
                        }
                    )
                    icl.note("Arm shifted while planning; the action was discarded")
                    after = time.monotonic()
                    continue
                if not new_batch and expected_qpos is not None:
                    drift = max(abs(a - b) for a, b in zip(current["qpos"][:5], expected_qpos[:5], strict=True))
                    if drift > 0.03:
                        pending = []
                        icl.note("Chunk tracking shifted; the remaining batch was discarded")
                        after = time.monotonic()
                        continue

                decision = pending.pop(0)
                action = decision["action"]
                if action == "stop":
                    self.fail(decision["reason"])
                if verified and action not in ("done", "observe"):
                    self.fail("Completion verification cannot issue another motion")
                becomes, refused = holding, None
                try:
                    becomes = next_holding(action, holding)
                    if action in ("ee_absolute", "ee_delta"):
                        check_reachable_move(ee_target(decision, current), current)
                except ValueError as exc:
                    refused = str(exc)

                entry = {
                    "step": step,
                    "decision": decision,
                    "holding": holding,
                    "new_model_batch": new_batch,
                    "remaining_batch": len(pending),
                    "observation": {k: v for k, v in observation.items() if k != "images"},
                }
                (run / "phase_map.json").write_text(json.dumps(policy.phase_map))
                self.feedback(decision["reason"])
                icl.step(
                    step,
                    decision,
                    entry["observation"],
                    observation["images"],
                    latency,
                    policy.phase,
                    batch={"new": new_batch, "remaining": len(pending)},
                )

                if refused is not None:
                    pending = []
                    failures += 1
                    self.feedback(refused)
                    entry["execution"] = {
                        "status": "rejected",
                        "reason": refused + stuck_hint(failures),
                        "measured_pose": current["pose"],
                        "measured_qpos": current["qpos"],
                    }
                elif action in ("joint_step", "ee_absolute", "ee_delta", "base_step"):
                    if action == "joint_step":
                        target = joint_target(decision, current)
                    elif action in ("ee_absolute", "ee_delta"):
                        target = ee_target(decision, current)
                    else:
                        target = decision["pose"]
                    # A base_step is a distance from wherever the base now stands, so it
                    # is never the same target twice; only arm targets are remembered.
                    key = (action, *[round(v, 4) for v in target])
                    if action != "base_step" and key in rejected:
                        outcome = {
                            "status": "rejected",
                            "reason": "This target already failed; change the approach",
                            "measured_pose": current["pose"],
                            "measured_qpos": current["qpos"],
                        }
                    elif action == "joint_step":
                        outcome = self._try_joint_step(decision, current, monitor, xml)
                    elif action in ("ee_absolute", "ee_delta"):
                        outcome = self._try_move(target, current, monitor, xml)
                    else:
                        outcome = self._try_base_step(decision["pose"][0], current, monitor, xml)
                    expected_qpos = outcome["measured_qpos"] if action == "joint_step" else None
                    entry["execution"] = outcome
                    if outcome["status"] == "reached":
                        failures = 0
                        if action == "base_step":
                            rejected.clear()  # the base carried every arm target with it
                    else:
                        pending = []
                        failures += 1
                        if action != "base_step":
                            rejected.add(key)
                        self.feedback(outcome["reason"] + "; replanning from measured state")
                        outcome["reason"] += stuck_hint(failures)
                elif action in ("open_gripper", "close_gripper"):
                    # The latch moves before the hardware: a failed open must not leave
                    # the run believing the item is gone. A failed close is recoverable —
                    # open on it, then close again.
                    holding = becomes
                    entry["execution"] = (
                        self._open(monitor, xml) if action == "open_gripper" else self._close(monitor, xml)
                    )
                    entry["execution"]["holding"] = holding
                    rejected.clear()
                    expected_qpos = None
                elif action == "retry":
                    policy.restart_attempt()
                    pending = []
                    rejected.clear()
                    verified = 0
                elif action == "done":
                    verified += 1

                if action not in ("done", "observe"):
                    verified = 0
                if "execution" in entry:
                    icl.execution(step, entry["execution"])
                history.append(entry)
                with (run / "trace.jsonl").open("a") as f:
                    f.write(json.dumps(entry, allow_nan=False) + "\n")
                with (run / "inspection.jsonl").open("a") as f:
                    f.write(json.dumps(policy.last_trace) + "\n")
                if verified >= 2:
                    succeeded = True
                    ending = "Task reported complete with an empty gripper and two confirming looks"
                    return SkillOutput(ending + ".", {"demonstration": str(demo.path), "trace": str(run)})
                after = time.monotonic()
                self.sleep(0.5 if action == "done" else 0.1)
            self.fail("Slash-and-pick action budget exhausted")
        finally:
            icl.end(succeeded, self.cancelled, ending)
            # Whatever is held stays held on an abort; never reopen on the way out.
            try:
                self.manipulation.halt()
            finally:
                self.mobility.stop()
                self.manipulation.safety.max_ee_speed = previous_speed
                if monitor is not None:
                    monitor.close()


class SlashAndPickCactusNoPrompt(_SlashAndPickCactus):
    """Infer the task from the recorded demonstration alone and carry it out.
    The model is told how to read an in-context demonstration and nothing about
    what the task is. Half of an ablation against slash_and_pick_cactus_with_prompt,
    which is the same skill with the task spelled out. Supervised, arm-only apart
    from small base adjustments; the run starts holding the prop and will release
    it. Clear the workspace and keep a hand on Stop.
    """

    object_description = ""

    def execute(
        self,
        demonstration: str = DEMONSTRATION,
        chunk_size: int = 1,
        overview_frames: int = 24,
        frame_selection: Literal["both", "keyframes", "uniform"] = "both",
        joint_step: bool = True,
        ee_absolute: bool = True,
        ee_delta: bool = False,
        base_step: bool = True,
    ) -> SkillOutput:
        motions = {"joint_step": joint_step, "ee_absolute": ee_absolute, "ee_delta": ee_delta, "base_step": base_step}
        return self._run(demonstration, chunk_size, overview_frames, frame_selection, motions)


class SlashAndPickCactusWithPrompt(_SlashAndPickCactus):
    """Slash the prop cactus with the held toy lightsaber, drop the saber, then
    pick the cactus up and put it back on its box. Same implementation as
    slash_and_pick_cactus_no_prompt, with the task stated in the prompt as well as
    shown in the demonstration. Supervised, arm-only apart from small base
    adjustments; the run starts holding the prop and will release it. Clear the
    workspace and keep a hand on Stop.
    """

    object_description = "prop cactus on a box, and a plastic toy lightsaber currently held"

    def execute(
        self,
        demonstration: str = DEMONSTRATION,
        chunk_size: int = 1,
        overview_frames: int = 24,
        frame_selection: Literal["both", "keyframes", "uniform"] = "both",
        joint_step: bool = True,
        ee_absolute: bool = True,
        ee_delta: bool = False,
        base_step: bool = True,
    ) -> SkillOutput:
        motions = {"joint_step": joint_step, "ee_absolute": ee_absolute, "ee_delta": ee_delta, "base_step": base_step}
        return self._run(demonstration, chunk_size, overview_frames, frame_selection, motions)
