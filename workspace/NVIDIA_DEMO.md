# NVIDIA sock demos

The supported agents are `qwen_socks_demo_3` (ArUco basket) and
`qwen_socks_demo_3_features` (natural basket features). Both use the same Qwen
model and app-only instructions, wait for skill completion, accept stop, and
support waving after the running skill finishes. Neither enables microphone
input or gaze. The feature agent is explicit wiring of the feature skills;
physical pickup/drop validation remains required before using it for a demo.

| Agent | Pickup | Drop |
| --- | --- | --- |
| `qwen_socks_demo_3` | `pick_sock_pile_yoloe` | `drop_in_box_aruco` |
| `qwen_socks_demo_3_features` | `pick_sock_pile_yoloe_features` | `drop_in_box_features` |

Both also expose `wave` and `turn_in_place`. A failed pickup must not trigger a
drop; a failed skill waits for operator instructions. Keep the basket stationary
for feature-based exclusion and docking. No new motion protections are added by
the agent/configuration cleanup.

## Deployment configuration

Keep detector routing in ignored local files:

- `config/sock_detector.local.json`: `{"url":"http://<detector-host>:8765","api_key":""}`
- `config/basket_features/runtime.local.json`: `{"api_url":"http://<basket-host>:9071"}`

These paths are relative to `workspace/`. A local JSON object **replaces** the
tracked JSON object; keys are not merged, so credentials for a different endpoint
are not inherited. Add `token_file` to the basket configuration when its endpoint
requires authentication. Keep tokens outside Git. Invalid local JSON fails
rather than silently using another server. Missing local files use the tracked
defaults. Existing `SOCK_YOLOE_URL`/`SOCK_YOLOE_API_KEY` and
`BASKET_POSE_API_URL`/`BASKET_POSE_API_TOKEN_FILE` environment settings still apply.
The Qwen endpoint and key remain in the robot's existing LLM settings.

Keep `config/box_marker.json` for marker teaching, and `config/basket_features/`
for reference images, calibration, detector requirements, and validation data.
`scripts/basket_pose_api.py`, `scripts/basket_pose_probe.py`, `basket_capture`,
and `teach_box_marker` support calibration and diagnostics; they are not obsolete
attempts. See [basket detector documentation](config/basket_features/README.md).

## Verification

In a ROS Humble environment with the workspace and Python clients available:

```sh
python3 -m pytest -q tests/test_nvidia_demo_agents.py tests/test_demo_config.py \
  tests/test_basket_features.py tests/test_basket_pose_api.py \
  tests/test_drop_in_box_features.py tests/test_feature_basket_exclusion.py
```

The agent tests use real workspace discovery and skill registration without
creating hardware nodes or issuing motion. Before deployment, rehearse both
agents on hardware: pickup, basket approach/drop, repeat, stop during pickup and
drop, failure recovery, and wave/app instructions. Local tests cannot establish
physical grasp quality, calibration accuracy, or successful basket landing.

## Retired attempts

The standalone `pick_sock_yoloe` tool is retired; its shared implementation is
now a private base for the two supported pile-pickup tools.

Earlier Gemini, stationary, mapped, single-sock Qwen, Demo 2, and LEGO-specific
agent attempts have been removed. Their source and old configuration snapshots
remain in Git history. The general `demo_agent` and its LEGO pickup tool remain
available, as do unrelated custom agents. Shared pickup, ArUco, navigation, and
arm helpers remain implementation dependencies of the two supported sock agents.

## Small toy pickup

`pick_up_pile_little_toy` is an operator-invoked skill for the colorful soft toys
in the floor pile. It locates a pile with YOLOE, inspects a padded native-image
crop for individual toys, and maps the selected mask point back into the normal
640x480 tracking coordinates. Boxes covering at least half the pile box are
rejected; an unresolved pile returns to search instead of using its center.
The existing instance-memory gate keeps re-detections on the selected toy.
The skill retains the ArUco basket exclusion and existing approach/grip settings.
Finger spacing confirms obstruction, not an exact one-toy count; toy grasp
quality and grip calibration need physical rehearsal. The sock agents retain
their original tool lists; this skill is separately selectable.

`pick_up_pile_little_toy_astra` is the alternative detector: pinned
`openai:gpt-6-astra`, low reasoning, `service_tier=ultrafast`, through the OpenAI
Responses route (independent of the Qwen RunPod endpoint). It sends one settled
high-resolution frame and requires the same native-pixel JSON fields consumed
from YOLOE: `image.{width,height}` and `detections` containing `confidence`,
`bbox_xyxy`, and `grasp_point_xy`. Astra supplies a visually estimated grasp point,
not a segmentation mask. Output is validated and converted to the same 640x480
candidate tuples and reference image. Basket exclusion is applied before initial
selection by projected floor distance. Subsequent looks use the existing target
identity gate; empty scenes resume search, while missing/invalid API results fail
without producing a grasp target. No YOLOE request is used by this skill.

Logs include request and complete detector-stage times under `[AstraToyPickup]`.
Three perception-only runs on MARS-47 measured requests at 5.74/3.73/3.34 seconds,
and total detector times at 6.53/4.56/4.07 seconds. All three responses confirmed
Ultrafast. These are a small sample through the Innate proxy, not a latency SLA;
the first call also renewed authentication. The pile was partly clipped by the
camera edge and independent initial selections varied between neighboring toys.
The existing sock grip threshold and carry motion remain in use: this detector
alternative does not resolve the toy slipping/empty-grip verification issue.
