# Battery demonstration agent

Select **pick_and_hand_battery_agent** in Blue's skill menu. The default episode
is `workspace/custom_skills/pick-and-hand-battery/raw_data/episode_0.h5` under
`/home/jetson1/innate-os`. The processed `data/` file does not contain images.
`object_description` defaults to presenting the black battery toward the other robot.

This is a new pickup attempt: start with a healthy arm, an open empty gripper,
a stationary base and a clear supervised workspace. Do not launch it to resume
an already-held battery. Grip strength is 0.5. The skill holds forward without
releasing or controlling the receiving robot. Blue's recurring servo 2 overload
must be resolved before another physical trial; this skill never reboots a servo.

Astra receives a visual overview with synchronized telemetry. It must call
`inspect_demo` for detailed source frames and then `record_phases` before `act`.
The phase map contains ordered intervals, inspected reference frames and visible
completion conditions. Each action cites an inspected frame in its current phase.
The model can advance one phase after observing completion evidence, or inspect
more frames. A normal later step takes one model call; initial analysis can take
up to four calls. Frames are loaded on demand, not the whole uncompressed video.
Phase state persists within a run; it is inferred again for a new run.

The battery episode contains a wrist-camera stall of up to 1.8 seconds. Inspection
labels each image's source timestamp, its offset from the requested row, and the
nearest measured arm pose/gripper target. It does not invent missing images or
pair a stale image silently with a later pose. Live observations still require
fresh, synchronized cameras. The original fixed-frame skill's strict row pairing
is unchanged.

Physical actions use the existing guarded execution loop: 4 cm/0.2 rad steps,
3 cm/s speed cap, fresh health/base/state checks after model latency, reachability
and measured tracking checks, cancellation, and retained grip on failure. These
are not a geometric collision planner. An already-issued goto cannot be preempted.
See `gesture-imitation.md` for the execution limits.

Runs save `phase_map.json`, `inspection.jsonl` (model tool calls), live camera
frames, and action/observation `trace.jsonl` under
`workspace/custom_skills/.gesture_runs/<run-id>/`. Images and demonstration
context are sent through the configured Innate OpenAI proxy to gpt-6-astra.

Validation on Blue: 18 tests passed, including arbitrary real-episode inspection,
phase gating, inspection budget, rejection of an existing grasp, and the real
execution loop with fake actuators for success, cancellation and failure cases.
Nine existing recorder-fixture tests were skipped in the staging environment.
A real read-only Astra preview requested frames around closure/lift, inferred five
phases, then proposed one action. No actuator was connected to that preview.
Physical execution of this new autonomous skill has not been tested.
