// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Robot-facing topic names and protocol constants. Ported subset of the
// mobile app's rosConstants.ts — keep names identical to the robot's.

export const ROSBRIDGE_PORT = 9090;

// Drive: {x, y} in [-1, 1], y > 0 = forward. The robot's mars_app node shapes
// each message into a velocity *target*; a 50 Hz smoother there ramps
// /cmd_vel_teleop toward it under acceleration limits, so what we publish is a
// request, not the twist the motors see.
export const JOYSTICK_TOPIC = "/joystick";

// ---- Manual-drive speed mode ----------------------------------------------
// A multiplier on the robot's configured motion_control speed caps, held as a
// live ROS parameter on mars_app (rcl_interfaces/srv/SetParameters, PARAMETER_DOUBLE).
// It scales the caps rather than replacing them, so a client can only ever slow
// the robot down, never drive it past what settings.yaml allows.
export const APP_CONTROL_NODE = "/mars_app";
export const SET_PARAMETERS_SERVICE = `${APP_CONTROL_NODE}/set_parameters`;
export const SPEED_SCALE_PARAM = "motion_control.speed_scale";
export const PARAMETER_DOUBLE = 3;
// The picker's presets. `scale` must match the mobile app's table so the two
// clients label the same robot state identically — the value is global, and
// whichever client changed it last wins.
export const SPEED_MODES = [
  { id: "slow", label: "Slow", scale: 0.35 },
  { id: "medium", label: "Med", scale: 0.65 },
  { id: "fast", label: "Fast", scale: 1.0 },
];

// Head tilt: std_msgs/Int32 degrees in [HEAD_MIN_DEG, HEAD_MAX_DEG].
export const HEAD_SET_POSITION_TOPIC = "/mars/head/set_position";
// Current position: std_msgs/String carrying JSON (HeadPosition).
export const HEAD_CURRENT_POSITION_TOPIC = "/mars/head/current_position";

export const TTS_TOPIC = "/brain/tts";
export const TTS_AUDIO_TOPIC = "/tts/audio";
export const TTS_PLAYBACK_TOPIC = "/tts/playback";

// Sim only: the operator's microphone as base64 PCM16 mono at 24 kHz
// (std_msgs/String). MicroInput reads this instead of arecord, mirroring
// /tts/audio where the browser is the robot's speaker.
export const MIC_AUDIO_TOPIC = "/mic/audio";

// Operator <-> brain chat. Send: std_msgs/String whose data is JSON
// {text, sender:"user", timestamp}. Receive: std_msgs/String whose data is JSON
// {sender, text, timestamp, ...} — sender "user" echoes the operator, "robot" is
// the agent's reply. Same topics the sim console + mobile app use.
export const CHAT_IN_TOPIC = "/brain/chat_in";
export const CHAT_OUT_TOPIC = "/brain/chat_out";
// UI-only VAD and speech pipeline diagnostics. Detailed chat renders
// speech_debug frames; the Brain page also consumes live vad_status frames.
export const INPUT_TELEMETRY_TOPIC = "/input_manager/telemetry";
// Full chat history snapshot (brain_messages/srv/GetChatHistory → {history}, a
// JSON string of {sender, text, timestamp, ...} entries). Fetched on connect so
// the panel shows the conversation from before this page load, not just live.
export const GET_CHAT_HISTORY_SERVICE = "/brain/get_chat_history";

// Agent/directive selection. The roster comes from a service (response carries a
// JSON-in-String `directives` array — directives[0] is {agents:[{id,
// display_name,...}]} or a bare agent array — plus `current_directive`).
// Selecting an agent publishes its id on /brain/set_directive and activates the
// brain; "None" deactivates it (std_srvs/SetBool).
export const GET_AVAILABLE_DIRECTIVES_SERVICE = "/brain/get_available_directives";
export const SET_DIRECTIVE_TOPIC = "/brain/set_directive";
export const SET_BRAIN_ACTIVE_SERVICE = "/brain/set_brain_active";
// Set which skills the current directive may use (std_msgs/String JSON:
// {agent_id, skills:[id,...]} — the full active set, not a delta).
export const SET_ACTIVE_SKILLS_TOPIC = "/brain/set_active_skills";
// Live agent state pushed by the brain (std_msgs/String JSON: {brain_active,
// current_directive, active_skills:[id,...]}). Latched + ~3s heartbeat; this is
// how a stop/start/directive change made from another client reaches this UI.
export const AGENT_STATUS_TOPIC = "/brain/agent_status";
// Lightweight 0–1 person pose geometry and lock state for the Agent camera overlay.
export const GAZE_TOPIC = "/brain/gaze";
// Deterministically start/stop the currently locked person follow (std_srvs/SetBool).
export const SET_FOLLOWING_SERVICE = "/brain/set_following";
// Reset the agent's brain/memory (brain_messages/srv/ResetBrain → {success}).
export const RESET_BRAIN_SERVICE = "/brain/reset_brain";
// Cloud/local agent backend connection (std_msgs/String JSON: {state, connected,
// message, uri, hosted, timestamp}) — distinct from the rosbridge link.
export const WEBSOCKET_STATUS_TOPIC = "/brain/websocket_status";

// Navigation map + odometry for the 2D map page.
export const MAP_TOPIC = "/map"; // nav_msgs/OccupancyGrid
export const ODOM_TOPIC = "/odom"; // nav_msgs/Odometry
// nav_msgs/Path — the planner's route. Both planner servers are namespaced;
// there is no root /plan publisher.
export const PLAN_TOPICS = ["/navigation/plan", "/mapfree/plan"];
// AMCL's map-frame pose estimate (geometry_msgs/PoseWithCovarianceStamped).
export const AMCL_POSE_TOPIC = "/amcl_pose";
// The exact goal navigate_to_position commanded (geometry_msgs/PoseStamped, latched).
export const COMMANDED_GOAL_TOPIC = "/nav/commanded_goal";
// Stop all active navigation (std_srvs/Trigger), no matter which client started it.
export const CANCEL_NAVIGATION_SERVICE = "/nav/cancel_navigation";
// Auto-localization (std_srvs/Trigger on grid_localizer). Can take tens of seconds.
export const LOCALIZE_SERVICE = "/localize";
// grid_localizer's one-shot result (std_msgs/String, published once per
// localization attempt): processing_map | localized | localized_low_confidence
// | error. Only seen if subscribed when it fires — steady-state localization
// health comes from /amcl_pose covariance instead (mobile-app pattern).
export const LOCALIZATION_STATUS_TOPIC = "/localization/status";
// AMCL's manual seed (nav2_msgs/srv/SetInitialPose) — place the robot by hand.
export const SET_INITIAL_POSE_SERVICE = "/set_initial_pose";

// ---- Nav page (live sensor panel) ------------------------------------------
// RPLidar scan, throttled to ~6 Hz on the robot (frame base_laser).
export const SCAN_TOPIC = "/scan"; // sensor_msgs/LaserScan
// The velocity actually sent to the base — output of the cmd_vel priority mux.
export const CMD_VEL_TOPIC = "/cmd_vel"; // geometry_msgs/Twist
// Nav2 global costmap (map frame; the planner runs under the "navigation"
// namespace). The local costmap lives in the odom frame, so it can't be
// overlaid on the map canvas without a live map->odom transform — skipped.
export const GLOBAL_COSTMAP_TOPIC = "/navigation/global_costmap/costmap"; // nav_msgs/OccupancyGrid
// Controller's 6x6 m rolling window (nav_msgs/OccupancyGrid). Odom-framed —
// the map widget places it via the current map<-odom correction.
export const LOCAL_COSTMAP_TOPIC = "/local_costmap/costmap";
// Latched static transforms; carries base_link -> base_laser from the URDF.
export const TF_STATIC_TOPIC = "/tf_static"; // tf2_msgs/TFMessage
// mode_manager state (std_msgs/String each): navigation/mapfree/mapping, and
// the active map's name.
export const NAV_CURRENT_MODE_TOPIC = "/nav/current_mode";
export const NAV_CURRENT_MAP_TOPIC = "/nav/current_map";
// std_msgs/String carrying JSON {"available_maps": ["home.yaml", ...]}.
export const NAV_AVAILABLE_MAPS_TOPIC = "/nav/available_maps";
// mode_manager services (brain_messages srvs, all → {success, message}):
// change_mode {mode: "navigation"|"mapping"|"mapfree"} swaps the whole nav
// lifecycle stack; save_map {map_name, overwrite} only works in mapping mode
// (name must be alphanumeric/_/-; ".yaml" is appended server-side);
// delete_map accepts base name or filename.
export const NAV_CHANGE_MODE_SERVICE = "/nav/change_mode";
export const NAV_CHANGE_MAP_SERVICE = "/nav/change_navigation_map";
export const NAV_SAVE_MAP_SERVICE = "/nav/save_map";
export const NAV_DELETE_MAP_SERVICE = "/nav/delete_map";
// map-frame pose while mapping (nav_msgs/Odometry, TF map->base_link),
// published by mode_manager only in mapping mode. The right pose source
// during SLAM: raw /odom drifts against the growing map, and /amcl_pose is
// stale from navigation mode.
export const MAPPING_POSE_TOPIC = "/mapping_pose";

// ---- Spatial memory ---------------------------------------------------------
// The robot's per-map visual memory (brain_client/memory): places it remembered
// while driving well-localized. Positions mirror latched, published on change
// (std_msgs/String JSON {map, fingerprint,
// cache: warm|cold|inline|unsupported|off, positions:[{id,x,y,theta,stamp}]});
// the JPEG behind each entry is served same-origin by the front door at
// /memory/image?map=…&id=… (webapp/proxy/media_routes.py).
export const MEMORY_POSITIONS_TOPIC = "/brain/memory_positions";
export const CLEAR_MEMORIES_SERVICE = "/brain/clear_memories";
// Per-memory delete (brain_messages/ForgetMemory: memory_id + the positions
// payload's fingerprint — a stale tab must not delete from a re-made map
// whose ids restarted).
export const FORGET_MEMORY_SERVICE = "/brain/forget_memory";
// One latched message per finished memory search (std_msgs/String JSON:
// {query, found, id?, x?, y?, theta?, seen_stamp?, explanation?, error?,
// latency_sec?, cached?, stamp}). Latched so a page opened just after the
// search still sees it; clients gate the animation on the payload's stamp.
export const MEMORY_SEARCH_TOPIC = "/brain/memory_search";

// Skill-execution status (std_msgs/String JSON: {primitive_name|skill_name,
// status: running|completed|failed|interrupted, primitive_id, ...}), published
// as the agent runs primitives. Separate from chat_out — the chat surfaces it so
// operators can see which skills the agent is executing.
export const SKILL_STATUS_UPDATE_TOPIC = "/brain/skill_status_update";

// Per-step ACT inference timing breakdown (std_msgs/String carrying JSON), published
// by the manipulation server while a learned behavior runs. Drives the Profiling page.
export const INFERENCE_PROFILE_TOPIC = "/brain/manipulation/inference_profile";

export const BATTERY_STATE_TOPIC = "/battery_state";
// std_msgs/String carrying JSON (RobotInfo).
export const ROBOT_INFO_TOPIC = "/robot/info";

// Robot speaker volume (mars_msgs/srv/SetVolume): request volume_percent 0–100,
// response {success, message}. Applies live (no restart). Same service the
// mobile app's volume slider calls; current value rides on /robot/info.
export const SET_VOLUME_SERVICE = "/set_volume";

// Power off the Jetson (mars_msgs/srv/Shutdown): request {delay_seconds},
// response {success, message}. mars_control runs `sudo shutdown` — the same
// service the mobile app's power-off calls.
export const SHUTDOWN_SERVICE = "/shutdown";

// WebRTC signaling over rosbridge. START is a std_msgs/String JSON payload that
// carries our client_id: {"source":"live","audio":bool,"client_id":"...","video":[...]}.
// The robot routes offer/answer/ICE on the *_id topics, each enveloped as {client_id, ...}
// so several devices can negotiate concurrently on the same topics. START stays a shared
// topic; the client_id lives in its payload, not the topic name.
export const WEBRTC_START_TOPIC = "/webrtc/start";
export const WEBRTC_OFFER_TOPIC = "/webrtc/offer_id";    // robot -> us: {client_id, sdp}
export const WEBRTC_ANSWER_TOPIC = "/webrtc/answer_id";  // us -> robot: {client_id, sdp}
export const WEBRTC_ICE_IN_TOPIC = "/webrtc/ice_in_id";  // us -> robot: {client_id, candidate, sdpMLineIndex, sdpMid}
export const WEBRTC_ICE_OUT_TOPIC = "/webrtc/ice_out_id"; // robot -> us: {client_id, candidate, sdpMLineIndex, sdpMid}
export const WEBRTC_ACTIVE_STREAMS_TOPIC = "/webrtc/active_streams"; // robot -> us: {count, clients[], cameras[]}

// Leader-arm teleop: raw Dynamixel ticks (Int32MultiArray, 6 servos) — the
// robot's mars_app converts (tick - 2048) * 2π/4096 into /mars/arm/commands.
// Same topic the mobile app uses for its non-UDP fallback path.
export const LEADER_POSITIONS_TOPIC = "/leader_positions";

// Reboot the arm servos (std_srvs/Trigger → {success, message}). Power-cycles
// and reconfigures all 7 servos (6 arm joints + head), recenters the head to
// 0°, re-torques the head, and leaves the *arm* limp. Same service the mobile
// app's "Reboot Arm Servos" button calls.
export const ARM_REBOOT_SERVICE = "/mars/arm/reboot";

// The reboot power-cycles the servos and "takes a few seconds" — give the
// service call more room than the 10s default before timing out.
export const ARM_REBOOT_TIMEOUT_MS = 20_000;

// One confirm for every reboot entry point (arm panel, protection alert).
export const ARM_REBOOT_CONFIRM =
  "Reboot the arm servos? Any running task stops, the head recenters to level, and torque re-enables automatically once the servos come back.";

// Enable/disable torque on the 6 arm servos (std_srvs/Trigger). torque_on syncs
// the target to the current pose first, so the arm holds where it is rather
// than snapping. A reboot leaves the arm torque-off.
export const ARM_TORQUE_ON_SERVICE = "/mars/arm/torque_on";
export const ARM_TORQUE_OFF_SERVICE = "/mars/arm/torque_off";

// Arm health/torque state (mars_msgs/ArmStatus → {is_ok, error,
// is_torque_enabled}), published ~0.2 Hz. Drives the live torque toggle.
export const ARM_STATUS_TOPIC = "/mars/arm/status";

// Skills pinned to the top of the Skills panel, in this order; matched against
// the skill's display name (case-insensitive, last path segment, "_"→" ").
// Skills not currently available are skipped, everything else keeps roster order.
// Ported from the sim console's config.json `pinnedSkills`.
export const PINNED_SKILLS = ["navigate with vision", "navigate with position", "wave"];

// Run one skill directly (brain_messages/action/ExecuteSkill). Goal is
// {skill_type, inputs} where inputs is a JSON object string; feedback streams
// {skill_type, feedback, image_b64}; result is {success, message, skill_type,
// success_type ∈ "success"|"cancelled"|"failure"}. Same action the sim console
// drives. Skill roster + per-skill input schema come from AVAILABLE_SKILLS_TOPIC.
export const EXECUTE_SKILL_ACTION = "/execute_skill";
export const EXECUTE_SKILL_ACTION_TYPE = "brain_messages/action/ExecuteSkill";
// Cancels the running skill regardless of which client sent its goal (action
// cancels only bind to the sender). std_srvs/Trigger.
export const CANCEL_SKILL_SERVICE = "/brain/cancel_skill";

export const HEAD_MIN_DEG = -40;
export const HEAD_MAX_DEG = 70;

// ---- Logging page ---------------------------------------------------------
// innate_console bridge (see innate-os/.../innate_console): raw tmux pane
// stdout live, plus on-request backlog replay. String payloads carrying JSON.
// This is the single log source — the Logging page parses node identity and
// severity out of the rcl lines, so /rosout (incomplete) isn't used.
export const CONSOLE_TOPIC = "/innate/console";
export const CONSOLE_REQUEST_TOPIC = "/innate/console/request";
export const CONSOLE_BACKFILL_TOPIC = "/innate/console/backfill";
// rosapi (rws): live node roster for the health panel.
export const NODES_SERVICE = "/rosapi/nodes";

// ---- Datasets page --------------------------------------------------------
// Live roster of skills the robot knows about (innate_msgs, JSON via rws):
// {skills: Skill[]}. Same topic the mobile app's SkillsContext subscribes to.
// Physical (recordable) skills carry a `directory` and an `episode_count`.
export const AVAILABLE_SKILLS_TOPIC = "/brain/available_skills";
// Per-skill dataset metadata: request {task_directory}, response
// {success, json_metadata, message} where json_metadata is a JSON-encoded
// TaskSummary (episodes[]). Mirrors the mobile app's getTaskMetadata.
export const GET_TASK_METADATA_SERVICE = "/brain/recorder/get_task_metadata";

// Background episode → H.264 encoder (dataset_encoder node). The service queues
// a raw episode for conversion ({task_directory, episode_id}); the latched topic
// reports progress (EncodeStatusMsg). Episode MP4s and joint data are then
// fetched as plain same-origin HTTP from the front door: GET /episode and
// GET /episode/joints (see proxy/https_server.py).
export const ENCODE_EPISODE_SERVICE = "/datasets/encode_episode";
export const ENCODE_STATUS_TOPIC = "/datasets/encode_status";

// Episode curation (recorder services). Outcome is a per-episode label
// ("success"/"failure"/"" to clear); delete is a hard delete of files+metadata.
export const SET_EPISODE_OUTCOME_SERVICE = "/brain/recorder/set_episode_outcome";
export const DELETE_EPISODE_SERVICE = "/brain/recorder/delete_episode";
// Copy an episode into another dataset — promotes an eval rollout into a
// training dataset. Files (h5, MP4s, profile trace) and provenance travel with
// it: {source_task_directory, episode_id, dest_task_directory} →
// {success, message, new_episode_id}. The source episode is left untouched.
export const COPY_EPISODE_SERVICE = "/brain/recorder/copy_episode";

// ---- Collect page ---------------------------------------------------------
// Live recording, mirroring the mobile app's RecordEpisodeScreen flow. The
// operator picks (or creates) a physical skill, the recorder is pointed at its
// dataset directory once via activate_physical_primitive, then the four
// new/stop/save/cancel services drive one episode at a time (empty bodies —
// activation already set the target directory server-side).
//
// create_physical_skill ({name} → {success, message, skill_directory, skill_id})
// makes a fresh dataset; available_skills then lists it. recorder/status is a
// latched live state topic ({skill_id, episode_number, status}).
export const RECORDER_STATUS_TOPIC = "/brain/recorder/status";
export const CREATE_PHYSICAL_SKILL_SERVICE = "/brain/create_physical_skill";
export const ACTIVATE_PHYSICAL_PRIMITIVE_SERVICE = "/brain/recorder/activate_physical_primitive";
export const RECORDER_NEW_EPISODE_SERVICE = "/brain/recorder/new_episode";
export const RECORDER_STOP_EPISODE_SERVICE = "/brain/recorder/stop_episode";
export const RECORDER_SAVE_EPISODE_SERVICE = "/brain/recorder/save_episode";
export const RECORDER_CANCEL_EPISODE_SERVICE = "/brain/recorder/cancel_episode";

// Recorded-movement ("replay"/"mimic") skills — record one motion and the robot
// plays it back deterministically, no training. The draft is a normal physical
// skill (create_physical_skill + activate, then one new/stop/save episode);
// save_as_replay_skill promotes it in place ({task_directory, name, guidelines,
// episode_id:-1} → {success, skill_directory, skill_id}). delete_skill removes an
// abandoned draft ({skill_directory}). Gated on RobotInfo.supports_digital_skills.
export const SAVE_AS_REPLAY_SKILL_SERVICE = "/brain/recorder/save_as_replay_skill";
export const DELETE_SKILL_SERVICE = "/brain/delete_skill";

// ---- Training page --------------------------------------------------------
// Cloud training via the innate_training node (auth handled on the robot). The
// job_statuses topic is latched (TrainingJobList): every submitted skill + its
// runs + transfer progress. Services mirror the mobile app's training flow.
export const JOB_STATUSES_TOPIC = "/innate_training/job_statuses";
export const SUBMIT_SKILL_SERVICE = "/innate_training/submit_skill";
export const CREATE_RUN_SERVICE = "/innate_training/create_run";
// Robot-owned: submit + create the run after upload, so the page can be closed
// right after calling. Preferred over orchestrating submit→create client-side.
export const START_TRAINING_SERVICE = "/innate_training/start_training";
export const DOWNLOAD_RESULTS_SERVICE = "/innate_training/download_results";
export const CANCEL_RUN_SERVICE = "/innate_training/cancel_run";
export const GET_TRAINING_STATUS_SERVICE = "/innate_training/get_training_status";
// Live training-log tail (GetRunLogs). Best-effort, only while a run is training.
export const GET_RUN_LOGS_SERVICE = "/innate_training/get_run_logs";
// Reload the brain so a freshly downloaded checkpoint goes live.
export const BRAIN_RELOAD_SERVICE = "/brain/reload";

// Friendly subpane names per tmux window — index = pane (0 = left, 1 = right).
// Robot windows mirror innate-os/scripts/launch_ros_in_tmux.sh ROS_COMMAND_GROUPS;
// sim windows mirror innate-os/scripts/launch_sim_in_tmux.zsh. Keep both in sync
// so the Logging page labels each subpane by launch file (in the sim and on the
// robot alike) instead of falling back to the raw "window.pane" id.
/** @type {Record<string, string[]>} */
export const PANE_LAUNCH_LABELS = {
  // Robot (launch_ros_in_tmux.sh)
  "app-bringup": ["app.launch.py", "mars_bringup.launch.py"],
  "arm-recorder": ["arm.launch.py", "recorder.launch.py"],
  "brain-nav": ["brain_client.launch.py", "mode_manager.launch.py"],
  "behaviors-inputs": ["behavior.launch.py", "input_manager.launch.py"],
  "cam-leader": ["camera_composable.launch.py", "udp_leader_receiver.launch.py"],
  "ik-logger": ["ik.launch.py", "logger.launch.py"],
  "training-uninavid": ["training_node", "uninavid.launch.py"],
  "console": ["console.launch.py", "dataset_encoder.launch.py"],
  // Sim (launch_sim_in_tmux.zsh) — different window split, .sim launch variants
  "zenoh": ["rmw_zenohd"],
  "rosbridge-app": ["sim_rosbridge.launch.py", "app.sim.launch.py"],
  "sim-driver": ["sim_driver.launch.py"],
  "nav-brain": ["mode_manager.launch.py", "brain_client.sim.launch.py"],
  "behavior": ["behavior.launch.py"],
  "arm-ik": ["ik.py"],
  "vision-nav": ["uninavid.launch.py"],
  "console-webapp": ["console.launch.py", "https_server.py"],
  "foxglove": ["foxglove_bridge_launch.xml"],
};

// ---- Camera Calibration page ----------------------------------------------
// Interactive stereo (ChArUco) calibration, driven by the mars_cam
// stereo_calibrator node. The action stays open for the whole capture session:
// goal {mode: MODE_MANUAL(0), num_images, min_corners, save_calibration},
// feedback streams after each capture attempt ({phase, images_captured,
// capture_attempts, corners_found, image_names, images: CompressedImage[]}),
// result lands once enough images are captured / the goal is cancelled / the
// server's capture watchdog times out ({success, message, images_captured,
// left_rms, right_rms, stereo_rms}).
export const RUN_STEREO_CALIBRATION_ACTION = "/mars/main_camera/run_stereo_calibration";
export const RUN_STEREO_CALIBRATION_ACTION_TYPE = "mars_msgs/action/RunStereoCalibration";
// Publishing a std_msgs/Bool here (while a goal is running) triggers one
// capture attempt against the current stereo frame. Must be a non-empty
// message type — rws cannot deserialize a zero-field std_msgs/Empty published
// from a browser client (crashes the subscriber node), so this is Bool not Empty.
export const STEREO_CALIB_CAPTURE_TOPIC = "/mars/main_camera/calib/enter_events";
// Server-side defaults, mirrored here as the page's number-input defaults.
export const STEREO_CALIB_DEFAULT_NUM_IMAGES = 20;
export const STEREO_CALIB_DEFAULT_MIN_CORNERS = 10;
// Depth is only published once a valid stereo calibration is loaded (the depth
// estimator gates on it), so whether a frame ever arrives here is a reliable,
// zero-new-ROS-code proxy for "the robot currently has a calibration file".
export const MAIN_CAMERA_DEPTH_TOPIC = "/mars/main_camera/depth/image_rect_raw";

export const LAST_IP_KEY = "innate.lastRobotIP";
