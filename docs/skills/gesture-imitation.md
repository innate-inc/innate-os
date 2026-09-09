# Demonstration-conditioned pickup and presentation

`imitate_pick_and_present` uses a recorded MARS gesture as in-context input to
GPT-6 Astra. Each decision includes recorded head/wrist keyframes, measured EE
poses and joints, the current scene and recent execution history. The model
chooses the next bounded movement or gripper action; there is no fitted model
and no fixed pickup trajectory. This is an experimental implementation, not a
reproduction of Wenli Xiao's unpublished Codex demonstration session.

## Record a demonstration

Use the existing gesture recording UI. Keep the base stationary, pick up the
object, lift it at least 4 cm and end holding it forward (EE x >= 20 cm). Stop
recording while still presenting it; do not include a return-to-rest or release
at the end. Use the normal main-left and wrist cameras and keep the workspace
clear for the first supervised trials.

New recordings retain `/action`, `/observations/qpos`, `/observations/qvel`,
images and their existing timestamps. They also store `/observations/ee_pose`
with rows `[x,y,z,qx,qy,qz,qw]` in metres and a unit quaternion, expressing
`ee_link` in `base_link`. Each row is FK of the **same measured joint sample**
as qpos, rather than a separately sampled `/fk_pose` message. Attributes retain
the full URDF, joint order, camera topics and pose conventions. The timestamp
is `/timestamps/arm`; the EE dataset participates in streaming rollback.

The current robot model must match the recorded model. The loader rejects
unknown frame/camera conventions, moving-base demonstrations, malformed state,
and selected camera frames more than 250 ms from their arm sample.

## Existing recordings

In the sourced robot environment, convert to a **new** file:

```sh
python3 -m innate.gesture /absolute/path/episode_0.h5 /absolute/path/episode_0_ee.h5 \
  --urdf /absolute/path/to/the/recording/robots/mars.urdf
```

Neither input nor an existing output is overwritten. Old files have no model
provenance: explicitly choose the URDF that applied when recording. Conversion
assumes the historical joint1–joint6 order and main-left/wrist camera order;
it cannot recover missing timestamps or prove old calibration. Alternatively,
pass `legacy_urdf` to the skill to derive poses in memory without changing files.

## Run the skill

After building this branch and restarting the skill server, select
`imitate_pick_and_present` in the skill menu:

- `demonstration`: the absolute path to the finalized `.h5` episode.
- `object_description`: e.g. `the blue cup`.
- `legacy_urdf`: leave blank for new recordings.

The existing Innate proxy must authorize OpenAI `/v1/responses` and `gpt-6-astra`.
The skill uses structured Responses output; it never substitutes Gemini or a
trained ACT policy. [API format reference](https://developers.openai.com/api/docs/guides/structured-outputs).

This first version sends up to 12 ordered keyframes from each camera, including
gripper transitions and the final state. It does not send a video file or all
frames on every turn. It chooses one action per observation (not arbitrary
Python execution), with at most 60 actions / ten minutes. For useful trials,
move the same object a few centimetres from its demonstrated location and
compare successful adaptation against unchanged joint replay.

The gripper remains closed after grasp and at completion. “Present” is not an
automatic handoff/release. Completion needs two fresh visual assessments of
retention/presentation plus a measured lift, forward position and proximity to
the demonstrated final position. This is visual verification, not an independent
contact sensor or a guarantee of success.

Each run saves input camera images, model decisions and observations under
`workspace/custom_skills/.gesture_runs/<run-id>/`. The model and demonstration
provenance are in `manifest.json`; action history is in `trace.jsonl`.

## Motion limits and current limitations

- Stationary base; stop on detected base motion, stale cameras/joints or bad arm health.
- Each target is within 4 cm / 0.2 rad per Euler component of the measured pose.
- Use existing IK and a 3 cm/s EE speed cap; reject unreachable targets rather
  than silently clamping them. Check measured positional tracking within 1.5 cm.
- No automatic reopening, resting or torque-off after a grasp, including errors/Stop.
- Existing goto services cannot preempt an already-issued movement. Stop prevents
  the next action; the single bounded in-flight movement may finish.
- Workspace limits and model visual clearance are **not** a geometric collision
  planner. Model latency also means objects/people can change after an image.
  Physical trials need supervision and a clear, controlled workspace.

## Local verification

In a ROS Humble environment with the repository's dependencies installed:

```sh
cd ros2_ws
colcon build --packages-select manipulation --cmake-args -DBUILD_TESTING=ON
cd ..
ROS_LOCALHOST_ONLY=1 ROS_DOMAIN_ID=87 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
GESTURE_RECORDER_FIXTURE="$PWD/ros2_ws/build/manipulation/record_ee_fixture" \
GESTURE_RECORDER_NODE="$PWD/ros2_ws/install/manipulation/lib/manipulation/recorder_node_cpp" \
python3 -m pytest tests/test_gesture_imitation.py -q
```

Tests cover the real recorder start/save services, C++/Python FK agreement,
streaming rollback, legacy conversion, demonstration conditioning, action limits,
ROS freshness, and the actual skill loop with fake model/actuator responses.
They do not establish live-model grasping ability or physical success.
