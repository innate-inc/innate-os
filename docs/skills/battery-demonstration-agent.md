# Battery demonstration agent

Select **pick_and_hand_battery_agent** in Blue's skill menu. The default episode
is `workspace/custom_skills/pick-and-hand-battery/raw_data/episode_0.h5` under
`/home/jetson1/innate-os`. The processed `data/` file does not contain images.
`object_description` defaults to presenting the black battery toward the other robot.

This is a new pickup attempt: start with a healthy arm, an open empty gripper,
a stationary base and a clear supervised workspace. Do not launch it to resume
an already-held battery. Grip strength is 0.5. The skill holds forward without
releasing or controlling the receiving robot. On a driver-reported servo hardware fault, the skill calls `/mars/arm/fix_error`,
which reboots and reconfigures only faulted servos. It requires a new healthy
status and fresh images/pose before replanning, preserving the grip latch and
discarding the interrupted action. At most two recoveries are allowed per run;
a failed or uncertain recovery stops the run. Rebooting clears a latched fault,
but does not fix the mechanical cause of a recurring overload.

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
3 cm/s speed cap, fresh health/state checks after model latency, reachability
and measured tracking checks, cancellation, and retained grip on failure. These
are not a geometric collision planner. An already-issued goto cannot be preempted.
See `gesture-imitation.md` for the execution limits.

Runs save `phase_map.json`, `inspection.jsonl` (model tool calls), live camera
frames, and action/observation `trace.jsonl` under
`workspace/custom_skills/.gesture_runs/<run-id>/`. Images and demonstration
context are sent through the configured Innate OpenAI proxy to gpt-6-astra.

The preceding physical run grasped and started lifting the battery, then stopped
with servo 2 overload. Automatic recovery has been tested with fake actuators
and service responses; a physical recovery during this skill is not yet verified.

## Motion feedback and priority

Requests now use low reasoning and priority processing; a read-only proxy check
confirmed the API returned the `fast` tier. This does not guarantee a fixed latency.

IK rejection returns `execution.status=unreachable` without motion. A completed
move missing the target by more than 1.5 cm returns `not_reached`. Both outcomes
include the requested EE pose, actual measured pose/joints and positional error
in the next agent turn, also saved to `execution.jsonl`. The agent must use that
feedback and fresh images to choose another approach. Exact failed targets are
blocked, and three failed proposals without a successful move stop the run.
Missing telemetry, cancellation and uncertain motion outcomes remain hard stops.
Driver-reported hardware faults use the targeted recovery described above.
The tracking threshold and driver joint limits are unchanged. Base displacement
and twist no longer abort this skill. An arm shift during model latency discards
the stale action and returns the measured state to the agent instead of aborting.

Validation: 33 applicable tests pass; nine recorder-fixture tests are skipped.
Coverage includes recovery after closing, retained grip, replanning feedback,
recovery budget, fresh post-reboot health, service failure/timeout and cancellation.
