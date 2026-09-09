# Tuning parameters

Two files, two jobs:

| File | What goes here | Format |
|------|----------------|--------|
| **`config/settings.yaml`** | How the robot **behaves** — tunable ROS parameters: driving speed, camera, arm, manipulation, navigation, brain runtime knobs, TTS voice, extra agent/skill dirs | native ROS 2 params YAML |
| **`.env`** | How to reach the cloud + **environment** — secrets (`INNATE_SERVICE_KEY`), all cloud endpoint URLs (`BRAIN_WEBSOCKET_URI`, `TELEMETRY_URL`, `UNINAVID_WS_URL`, `INNATE_PROXY_URL`, …) | dotenv |

Both are auto-created from their `.template` on update (and by `./innate-sim setup`); your edits are never overwritten.

> Cloud endpoint URLs all live in `.env` (one place to point the robot at a different
> backend) — several are read by client libraries outside the ROS param system, so they
> can't be ROS params anyway. (`config/os.toml` was removed; its values now live in these
> two files.)

## How `config/settings.yaml` works

It's a normal ROS 2 params file. ROS **layers it on top** of each node's own config (last-wins), so anything you set wins over the package default:

```python
# every launch does, in effect:
Node(parameters=[package_defaults.yaml, config/settings.yaml])   # last wins
```

To tune something, **uncomment a whole stanza** (the `node:`, `ros__parameters:`, and the value line together) and edit the value. `/**` applies to every node that declares the parameter; a named section targets one node. Restart the nodes (`innate restart`) to apply.

```yaml
/**:
  ros__parameters:
    motion_control:
      max_speed: 0.3        # was 0.4
```

> Notes: an all-commented file = "no overrides" (safe). Use decimals for float values
> (`0.3`, not `3`) — native YAML keeps the type you write. A key only takes effect if the
> node declares that parameter.

## What you can tune

| Stanza (node) | Keys | Default |
|---|---|---|
| `/**` `motion_control` (teleop) | `max_speed` / `max_angular_speed` | `0.4` / `1.0` |
| `/**` `nav` (autonomous nav2) | `max_speed` / `max_angular_speed` | `0.45` / `0.6` |
| `/**` `inflation_layer` | `inflation_radius` / `cost_scaling_factor` | `0.3` / `10.0` |
| `joystick_controller` `joystick` | `slow_mode_factor` | `0.25` |
| `bringup` `battery` | `warning_percentage` / `critical_percentage` | `20` / `10` |
| `bringup` `safety` (hard `/cmd_vel` clamp) | `max_speed` / `max_angular_speed` | `0.4` / `2.5` |
| `mars_arm` | `max_jerk` | `150.0` |
| `main_camera_driver` (hardware) | `width/height` (capture), `publish_stereo_width/height`, `publish_left_width/height` (published size; downstream follows it automatically), `fps`, `jpeg_quality`, `auto_exposure_mode`, `exposure`, `gain`, `default_gain`, `target_brightness`, `ae_kp`, `publish_native` / `native_fps` (the lazy native MJPG topic `/mars/main_camera/native/compressed` and its rate cap — `0` is unthrottled, every capture-resolution buffer; `publish_native: false` is what turns the topic off) | see template |
| `arm_camera_driver` (hardware) | `width/height` (capture), `fps` | see template |
| `webrtc_streamer` (teleop stream) | `main_width/main_height`, `arm_width/arm_height` (encode size per camera) | see template |
| `manipulation_server` | `inference_hz`, `speed`, `n_action_steps` (0=auto), `temporal_ensemble_coeff` | `25.0`, `1.5`, `0`, `0.0` |
| `navigation_grid_localizer` | `max_score_threshold`, `max_range`, `auto_localize_timeout` | `0.3`, `12.0`, `30.0` |
| `brain_client_node` | `cartesia_voice_id` (TTS voice), `vertical_fov`, `pose_image_interval`, `scan_stale_after_sec`, `send_depth`, `send_arm_camera_image`, `log_everything`, STT/transcribe models | see template |
| `people_node` (recognition + person memory; **experimental**, `enabled: true` turns it on at the next reboot) | `enabled` (launch-time only), `always_on`, `seek_faces`, `scribe`, `prefer_backend`, `tick_source`, `allow_model_download`, `retention_unnamed_days` / `retention_named_days`, `camera_height_m`, `gemini_model` | `false`, `false`, `false`, `true`, `"opencv"`, `"compressed"`, `true`, `14.0` / `548.0`, `0.26`, see template |
| `uninavid_node` (VLN) | `forward_speed`, `turn_speed`, `cmd_duration_sec`, `image_send_hz`, `consecutive_stops_to_complete`, `cmd_publish_hz`, `poll_period_sec` | `0.3` / `0.8`, rest see template |

> **Driving caps vs the safety clamp.** `motion_control` is the *driving feel* cap: the
> joystick, keyboard, and app drive joystick all ship the same `0.4` m/s / `1.0` rad/s
> default, so an uncommented-but-unedited `/** motion_control` stanza is a no-op. The
> *hard ceiling* is separate: bringup's `safety` clamp (`0.4` m/s / `2.5` rad/s) caps
> every `/cmd_vel` at the motors, so it is the one limit every source — teleop, nav2,
> brain — passes through. It is a backstop, deliberately kept above the driving caps, so
> tuning driving feel never lowers it. Keep `safety` ≥ `motion_control` and ≥ `nav`.

## The one bit of magic: the nav remap

Teleop and autonomy have **separate speed caps** so they tune independently — turn nav2 down for safety without throttling manual driving, or give manual control a more aggressive cap without speeding up autonomy. Native layering matches parameters **by name**: the joystick, keyboard, and app drive joystick all use `motion_control.max_speed`, so that one `/**` stanza reaches all three at once. **nav2** is the autonomous cap and names the same concept differently per component (`InnateFollowPath.vx_max`, `FollowPath.max_vel_x`, the smoother's `[x,y,theta]` list, costmap `inflation_layer`), so a thin remap translates the separate `nav` / `inflation_layer` knob onto those schemas at launch (`load_motion_limit_overrides` / `load_costmap_rewrites` in `mars_bringup/mars_bringup/config_loader.py`). Costmap inflation is applied via nav2 `RewrittenYaml`. Underneath all of them, bringup's own `safety` clamp is the overall ceiling on `/cmd_vel` — a separate backstop, not driven by `motion_control` — so keep `safety` ≥ `motion_control` and ≥ `nav`; even if a teleop or nav limit were missed, that clamp still enforces the real ceiling at the motors.

## Going deeper (advanced parameters)

Not surfaced in `settings.yaml` (edit the package YAML directly): per-joint arm PID gains (`mars_arm/config/arm_config.yaml`), full nav2 planner/controller/AMCL config (`mars_nav/config/`), stereo depth filters (`mars_cam`). Per-skill/model-coupled manipulation params (`chunk_size`, per-skill `n_action_steps`) live in each skill's `behavior_config`.

## Adding a knob

1. Add a commented stanza to `config/settings.yaml.template` targeting the node + parameter name.
2. Make sure that node's launch loads overrides last: `parameters=[pkg_yaml, *settings_params()]`.
   For a nav2 differently-named param, add a remap case in `config_loader.load_motion_limit_overrides`.
