# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Carry out the task shown in a recorded episode.

The recording is the model's context, not a trajectory to replay: there is no
trained policy, and every move is a fresh decision from the episode plus the
live cameras. `task` decides how much help it gets — left empty the task must be
inferred from the recording alone, filled in the same run is told what it is
looking at, which makes the pair an ablation on how much a demonstration carries
by itself. An inferring run therefore leaks nothing, in its observations or in
the schema it is handed.

Unlike a pickup skill this one deliberately RELEASES: the gripper may open to
drop one object and again to place another. A grip latch replaces the never-open
rule a carry skill relies on, enforcing only the ordering — no grasp with a full
gripper, no release with an empty one, no finishing while still carrying.
"""

import base64
import json
import math
import os
import time
import uuid
from pathlib import Path
from typing import Literal

from innate_skills.imitate_demonstration.imitation_runtime import ArmRuntime, LiveObservation

from innate import HeadState, MainImage, Manipulation, Mobility, Skill, SkillOutput, WristImage
from innate.demonstration import Demonstration, model_fingerprint
from innate.icl_trace import run_trace
from innate.imitation_actions import (
    FIXED_ACTIONS,
    MOTION_ACTIONS,
    check_reachable_move,
    ee_target,
    event_frames,
    joint_target,
    next_holding,
    parse_actions,
    stuck_hint,
)
from innate.imitation_policy import ImitationPolicy
from innate.imitation_prompt import build_instructions

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


DEMONSTRATION = ""


class _RunPolicy(ImitationPolicy):
    """The run's planner with this skill's action vocabulary."""


class ImitateDemonstration(Skill):
    """Watch a recorded episode and carry the same task out on the live scene.

    Leave `task` empty and the task must be inferred from the recording alone;
    fill it in and the same run is told what it is looking at. Supervised and
    arm-only apart from small base adjustments. The run may open the gripper, so
    anything held can be released deliberately. Start from a healthy arm on a
    clear workspace with a hand on Stop.
    """

    head_position: HeadState
    main_image: MainImage
    wrist_image: WristImage
    manipulation: Manipulation
    mobility: Mobility

    decision_timeout: float = 175
    grip_strength: float = 0.4

    # The task description for this run; empty means infer it from the recording.
    # It reaches the model in the prompt and in every observation, so it is the
    # one field that can give the answer away.
    task = ""
    overview_frames = 24
    shoulder_margin = 0.2  # rad of headroom over what the demonstration needed
    shoulder_hard_max = 0.5  # rad; the run that tripped the shoulder reached 1.04
    frame_selection = "both"
    survey = None
    allowed = (*MOTION_ACTIONS, *FIXED_ACTIONS)
    max_steps = 110
    time_budget_s = 1200

    def make_policy(self, demo):
        policy = _RunPolicy(demo, self.chunk_size)
        policy.allowed = self.allowed
        policy.instructions = build_instructions(self.allowed, self.task, self.chunk_size)
        # record_phases refuses to run before a detailed look; the forced looks
        # the skill hands it ARE that look.
        policy.inspected_detail = True
        if self.survey is not None:
            policy.add_look("uniform survey of the whole episode", self.survey.frames)
        return policy

    def _open(self, arm):
        self.manipulation.gripper_open(duration=0.8, block=False)
        arm.wait_motion()
        return {"status": "released", "reason": "Gripper opened; the item stays where it fell"}

    def _close(self, arm):
        self.manipulation.gripper_close(strength=self.grip_strength, duration=0.8, block=False)
        arm.wait_motion()
        measured = arm.observe(time.monotonic() - 0.2)
        return {
            "status": "closed",
            "reason": "Gripper closed; confirm acquisition from aperture and fresh images before lifting",
            "gripper": measured["gripper"],
            "measured_pose": measured["pose"],
        }

    def _run(self, demonstration, task, legacy_urdf, chunk_size, overview_frames, frame_selection, motions):
        from ament_index_python.packages import get_package_share_directory

        if type(chunk_size) is not int or not 1 <= chunk_size <= 10:
            self.fail("chunk_size must be an integer from 1 to 10")
        self.chunk_size = chunk_size
        if type(overview_frames) is not int or not 6 <= overview_frames <= 48:
            self.fail("overview_frames must be an integer from 6 to 48")
        if frame_selection not in ("both", "keyframes", "uniform"):
            self.fail("frame_selection must be both, keyframes or uniform")
        self.task = task.strip()
        try:
            self.allowed = parse_actions(**motions)
        except ValueError as exc:
            self.fail(str(exc))
        self.frame_selection = frame_selection
        # A recording made before the recorder stored its own kinematics has no
        # ee_pose; naming the model it was made with derives them in memory.
        urdf_arg = {"legacy_urdf": legacy_urdf} if legacy_urdf else {}
        demo = Demonstration(
            demonstration,
            image_time_reference=True,
            max_frames=overview_frames,
            uniform=frame_selection == "uniform",
            **urdf_arg,
        )
        # "both" is two looks: the evenly binned survey, then the gripper
        # transitions sampled closely. Loading twice costs about half a second.
        self.survey = None
        if frame_selection == "both":
            self.survey = Demonstration(
                demonstration, image_time_reference=True, max_frames=overview_frames, uniform=True, **urdf_arg
            )
            close = event_frames(self.survey.grip_events, len(self.survey.poses))
            if close:
                demo = Demonstration(demonstration, image_time_reference=True, frame_indices=close, **urdf_arg)
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
        if model_fingerprint(xml) != demo.model_hash:
            self.fail("Demonstration robot model differs from the running robot")
        policy = self.make_policy(demo)
        root = Path(os.environ.get("INNATE_OS_ROOT", Path(__file__).resolve().parents[2]))
        run = root / "workspace/custom_skills/.imitation_runs" / ("slashpick-" + uuid.uuid4().hex)
        run.mkdir(parents=True)
        (run / "manifest.json").write_text(
            json.dumps(
                {
                    "skill": self.name,
                    "demonstration": str(demo.path),
                    "model": "gpt-6-astra",
                    "model_sha256": demo.model_hash,
                    "chunk_size": chunk_size,
                    "task": self.task,
                    "route": policy.route,
                    "frame_selection": frame_selection,
                    "overview_frames": overview_frames,
                    "motion_actions": list(self.allowed),
                }
            )
        )
        icl = run_trace(self, run.name)
        icl.begin(
            "gpt-6-astra",
            str(demo.path),
            len(demo.poses),
            chunk_size=chunk_size,
            task=self.task,
            route=policy.route,
            frame_selection=frame_selection,
            motion_actions=list(self.allowed),
            overview=[f["index"] for f in demo.frames],
        )
        policy.trace = icl

        self._base_origin = None
        monitor = arm = None
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
            monitor = LiveObservation()
            arm = ArmRuntime(self, monitor, xml)
            started = after = time.monotonic()
            for step in range(self.max_steps):
                if time.monotonic() - started > self.time_budget_s:
                    self.fail("Slash-and-pick exceeded its time budget")
                observation = arm.observe(after)
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
                # A described run names the scene; an inferring run must not, or the
                # description would leak the answer the ablation is measuring.
                if self.task:
                    observation["task"] = self.task
                for name, image in observation["images"].items():
                    (run / f"{step:03d}_{name}.jpg").write_bytes(base64.b64decode(image))

                new_batch = not pending
                latency = 0.0
                if new_batch:
                    asked = time.monotonic()
                    values = arm.decide(policy, observation, history)
                    latency = time.monotonic() - asked
                    pending = list(values)
                self.check_cancelled()
                current = arm.observe(time.monotonic() - 0.2)
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
                        outcome = arm.try_joint_step(decision, current)
                    elif action in ("ee_absolute", "ee_delta"):
                        outcome = arm.try_move(target, current)
                    else:
                        outcome = arm.try_base_step(decision["pose"][0], current)
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
                    entry["execution"] = self._open(arm) if action == "open_gripper" else self._close(arm)
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
                if arm is not None:
                    arm.close()
                if monitor is not None:
                    monitor.close()

    def execute(
        self,
        demonstration: str = DEMONSTRATION,
        task: str = "",
        legacy_urdf: str = "",
        chunk_size: int = 1,
        overview_frames: int = 24,
        frame_selection: Literal["both", "keyframes", "uniform"] = "both",
        joint_step: bool = True,
        ee_absolute: bool = True,
        ee_delta: bool = False,
        base_step: bool = True,
    ) -> SkillOutput:
        motions = {"joint_step": joint_step, "ee_absolute": ee_absolute, "ee_delta": ee_delta, "base_step": base_step}
        return self._run(demonstration, task, legacy_urdf, chunk_size, overview_frames, frame_selection, motions)
