# Rehearsed sock demo

The current box tag was decoded from the live MARS 47 camera as DICT_4X4_50,
ID 1. Its measured outer black-square width is 80 mm, excluding the paper margin.
`TeachBoxMarker` defaults to those values. An ArUco board is not required.

## One-time setup

1. Attach the tag rigidly to the box, facing the approach direction. It must stay
   visible from the final parked position; use a rigid extension above the rim
   if the front face leaves the camera view. Do not move the tag relative to the box.
2. Keep the tag centered on the box's near/front face, at the plane of that
   face. The robot can teach from farther away; it does not need to be parked
   at the drop position. Keep the tag visible and the box still.
3. Run `innate-os/teach_box_marker` with defaults (`marker_id=1`,
   `marker_size_m=0.08`). It captures three stable views and derives a docking
   target with the front tag 23 cm ahead, centered at y=0. It never drives or
   moves the arm. A failed capture preserves prior teaching. Existing version-1
   teaching is converted in memory to this front-face target without losing
   the original recorded pose. Rehearse the arm path before autonomous use.
4. Select **Sock Rehearsed Demo** only after setup. Its drop skill is
   `innate-os/drop_in_box_aruco`. The existing **Sock Demo** remains available.

The runtime config is `workspace/config/box_marker.json`. Do not copy a taught
pose to another robot. Reteach after changing the tag, its mounting, camera
calibration or the rehearsed release geometry. Allow an unobstructed approach and space to rotate. If the tag is not visible,
the skill searches gently for up to one turn (35 seconds maximum) before driving.
This is a local docking controller, not route planning.

## Behavior and limits

- OpenCV detects only the configured ID and estimates pose from its four corners,
  known size, and the existing main-camera intrinsics. No Gemini calls for docking.
- Every approach uses decoded tag center and apparent size, as in FollowAruco.
  The 3D pose fit is used during teaching only; a runtime pose-fit rejection
  cannot turn a visible tag into a lost target.
  Missing/stale frames stop the base; duplicate IDs cannot establish a target.
  Three seconds without a fresh decoded tag during approach prevents release.
  A failed initial search leaves the robot holding the sock and waiting.
- Three fresh observations establish arrival at the taught pose. The arm follows
  the existing raise/reach/open/lift path, checking reachability and measured poses.
  Retreat starts only after measured rim clearance.
- Marker drop reports a release at the taught pose, not visually verified landing.
- Fast sock pickup/drop share a joint-only check: two fresh stable readings after a
  closing command. The MARS 47 empty cutoff is -0.03 rad (from prior observed empty
  grasps). A thin sock at 0.029 rad is held. Missing feedback is an error; a stalled
  gripper can still mimic a sock. Recalibrate after gripper changes.
- Empty pickup retries at the same spot up to three total grasps, without backing
  away or asking a model to judge the grasp. The standard rehearsed agent uses Gemini for sock detection; the Qwen variant
  uses Qwen for both agent decisions and sock detection.

Validation: synthetic marker images, PnP round-trip, offset docking simulation,
stale/lost/cancel cases, release/clearance ordering, and gripper retry tests. The
actual box image decodes and admits a pose estimate. Physical docking and landing
accuracy require the supervised rehearsal; no speed improvement is claimed yet.

## Shared follower controller

Marker docking now calls FollowAruco's actual `_drive_toward`, `_smooth`,
`_send_cmd` and `_stop` methods. The follower defaults and command sequence are
unchanged. Docking supplies the taught target center/size and a 4% size deadband,
then finishes on three fresh observations within 8 pixels horizontal, 20 pixels
vertical and 6% size error. Docking caps forward speed at 0.15 m/s, reverse at 0.08 m/s, and turning at
0.5 rad/s, with 0.067 m/s² linear and 0.267 rad/s² angular acceleration limits. Braking uses separate limits of 0.4 m/s² and
2.0 rad/s²; direction reversals brake to zero before gently accelerating again. The
post-clearance retreat is capped at 0.10 m/s. Standalone FollowAruco defaults
remain unchanged. There is no separate
turn-first phase, derivative controller or additional motion ramp.

## Qwen variant

Select **Qwen Sock Demo** to use `openai-chat:qwen3.8-flash-next-iq4-xs-mtp3`
for decisions and `pick_sock_qwen` for pickup. Set `llm_base_url` to your
OpenAI-compatible server's `/v1` endpoint in robot settings, and `LLM_API_KEY`
only if that server requires one. Restart the brain/skills launch after changing
the endpoint so the skills server reads the same setting as the agent.
No server address or credentials are shipped with the demo.

Both Qwen routes send `chat_template_kwargs.enable_thinking=false`. The model
server must support images, tool calls, and that chat-template option. This does
not change the Astra agent's request settings. Sock detection asks for the sock
on the floor, validates normalized boxes, and derives the grasp point from the
box center to avoid inconsistent point-coordinate ordering. Model correctness
is still required; box validation cannot prove that the target is a sock.

Run either pickup skill with a `prompt` describing one sock, including its color
(for example, `the blue sock`). The same description is reused during reacquisition.
Sock pickup keeps a 12 cm position gate and never discards its remembered target
to accept a distant candidate; a lost target causes failure instead of re-anchoring.
This cannot guarantee identity for adjacent identical socks or incorrect model detections. Marker drop is
`drop_in_box_aruco`, also without parameters. Stop both autonomous and manually
triggered skills before reloading code. The agent decides when to pick and drop;
there is no combined autonomous pickup-and-delivery skill.

The user reported a successful Qwen pickup on MARS 47. No matched repeatability
or throughput benchmark has been completed. Automated checks do not establish
physical reliability on different floors, boxes, socks, or gripper calibrations.

When the marker crosses from more than 24 pixels on one side to more than 24
pixels on the other, docking brakes translation and corrects with an in-place
turn from the unsmoothed horizontal error (capped at 0.25 rad/s). Three fresh
frames within 8 pixels end recovery. Missing observations reset recovery; it
never turns blindly from a stale marker. A five-second recovery timeout prevents
release when recentering fails.
