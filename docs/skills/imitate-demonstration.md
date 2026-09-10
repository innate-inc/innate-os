# Imitating a recorded demonstration

`imitate_demonstration` carries out the task shown in a recorded episode. There
is no trained policy and no replayed trajectory: the recording is the model's
context, and each move is a fresh decision from it plus the live cameras. Leave
`task` empty and the task must be inferred from the recording alone; fill it in
and the same run is told what it is looking at.

This is experimental and supervised. Run it on a clear workspace with a hand on
Stop.

## The recording

Any finalized `.h5` episode from the normal recorder works. It must carry
`/observations/qpos`, `/action`, both camera streams and `/timestamps/arm`, and
its joint order and camera mapping must match the robot running the skill.

End-effector poses come from `/observations/ee_pose` when the recording has it —
rows of `[x,y,z,qx,qy,qz,qw]` in metres, `ee_link` in `base_link`, each the
forward kinematics of the *same measured joint sample* as `qpos`, with the URDF
stored alongside. An older recording without it still loads: pass `legacy_urdf`
and the poses are derived in memory from the model you name. Nothing is written
back, so the original file is never modified.

The loader rejects a recording whose frame or camera convention it does not
recognise, whose state is malformed, or whose selected camera frames sit more
than 250 ms from their arm sample. A recording where the base drove is accepted:
see **Moving base** below.

## What the model is shown

Two forced looks before it may plan, then a window that follows the phase it is
executing.

1. **A uniform survey** across the whole episode, `overview_frames` of them. It
   must write down what it sees before the next look arrives.
2. **The gripper transitions**, sampled closely — three frames around each open
   or close. These are found by midpoint crossings of the gripper's own travel,
   which catches a slow grasp that a per-sample threshold misses entirely.
3. **`record_phases`**, once, committing two to six ordered phases with a frame
   interval and a visually observable completion condition for each.
4. **`act`**, every turn after that, shown the current phase sampled across its
   own interval plus the neighbouring phases' anchors — so the episode shows how
   a phase was performed, not merely that it happened.

Each frame arrives as both camera images plus `index`, `time_s`, `ee_pose`,
`qpos`, `gripper_target_rad` and `head_degrees`.

`frame_selection` chooses between `both` (the default), `uniform` alone or
`keyframes` alone, which is how you compare what the frame choice is worth.

## Motion

Which motions a run may command is selectable, and the prompt is rebuilt around
the selection — the model is never told about an action it cannot call.

| toggle | what it commands |
|---|---|
| `joint_step` | `[joint 1-5, delta radians]`, capped per step |
| `ee_absolute` | an absolute `[x,y,z,roll,pitch,yaw]` in `base_link` |
| `ee_delta` | the same target expressed relative to the measured pose |
| `base_step` | a straight base move, forward or back |

Gripper, observe, retry, done and stop are always available. Every cap is
defined once in `innate/imitation_actions.py`, and the prompt interpolates those
constants rather than restating them, so what the model is told cannot drift
from what is enforced.

Guards that stand between a proposal and the arm:

- Telemetry is re-read after the model call; a scene that moved during it
  discards the action rather than acting on stale state.
- Cartesian targets are checked against the workspace envelope and the per-step
  cap, and a target that moves sideways while it descends is refused — sliding
  across an object at low height tips it over before the fingers close.
- A joint target the driver would silently clamp is refused before anything moves.
- Motion never changes the gripper. Both the streaming and Cartesian paths carry
  j6 explicitly, so a move cannot drop what is held.
- A grip latch enforces ordering only: no grasp with a full gripper, no release
  with an empty one, no finishing while still carrying something.
- Base displacement, arm health and camera freshness are checked every turn.

These are not a geometric collision planner, and model latency means the scene
can change after an image. Physical trials need supervision.

## Moving base

A recording whose base moved is accepted. Recorded `ee_pose` is in `base_link`,
so where the base drove, the object's apparent motion is partly the base and not
the arm — copying that reach as arm travel is the failure this exists to
prevent. Frames from such an episode carry two extra fields:

- `base_command` — `[linear m/s, angular rad/s]`, the `/cmd_vel` row as recorded.
- `base_dead_reckoned` — `[x, y, yaw]` integrated from those commands.

`base_dead_reckoned` is **not measured odometry**: the recorder stores no base
pose, so this is forward-integrated command and wheel slip makes it an estimate.
Stationary recordings carry neither field.

## Watching a run

The In Context Learning page at `/icl` shows the demonstration being used, both
live cameras, and the model's reasoning as it arrives — the inferred phase
ladder, each decision with its numbers and latency, the two frames the model was
looking at when it decided, and the measured outcome once the arm reports back.
Runs start and stop from the same page.

It reads `/brain/icl_trace`, which every run publishes. Runs also save their
frames, phase map, decisions and outcomes under
`workspace/custom_skills/.imitation_runs/<run-id>/`.
