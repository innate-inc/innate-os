// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Shared types for the Innate webapp.
// Ambient declarations (no imports/exports), so every `// @ts-check`'d module
// can reference them in JSDoc without import clutter.

/** Connection lifecycle of the shared rosbridge socket. */
type ConnState = "disconnected" | "connecting" | "connected" | "reconnecting";

// ---- Training (innate_training node) --------------------------------------

/** builtin_interfaces/Time as delivered over rosbridge. */
interface RosTime {
  sec: number;
  nanosec: number;
}

/** Upload/download progress for a skill (upload) or run (download). */
interface TransferProgress {
  direction: number; // 0 UPLOAD, 1 DOWNLOAD
  stage: number; // 0 COMPRESSING,1 UPLOADING,2 DOWNLOADING,3 VERIFYING,4 DONE,5 ERROR
  filename: string;
  file_index: number;
  file_total: number;
  bytes_done: number;
  bytes_total: number;
  file_done: boolean;
  transfer_done: boolean;
  error: string;
  message: string;
}

/** One training run of a skill. status: 0 UNKNOWN,1 WAITING_FOR_APPROVAL,
 * 2 APPROVED,3 REJECTED,4 BOOTING,5 RUNNING,6 DONE,7 DOWNLOADED. */
interface TrainingRunStatus {
  run_id: number;
  status: number;
  daemon_state: string;
  error_message: string;
  started_at: RosTime;
  finished_at: RosTime;
  instance_type: string;
  /** Live training progress (parsed from the trainer's @@INNATE_PROGRESS marker).
   * Both 0 until the first marker arrives; total_step 0 => indeterminate. */
  current_step?: number;
  total_step?: number;
  has_active_transfer: boolean;
  transfer_done: boolean;
  active_transfer: TransferProgress;
  /** Optional W&B run URL (added later; absent in Phase 1). */
  wandb_url?: string;
  /** Run hyperparameters as a JSON string (training_params), for the detail
   * pane. Absent until the robot forwards it. */
  training_params_json?: string;
}

/** A submitted skill's training state (from /innate_training/job_statuses). */
interface TrainingSkillStatus {
  training_skill_id: string;
  skill_name: string;
  skill_dir: string;
  has_active_transfer: boolean;
  transfer_done: boolean;
  uploaded_episode_count: number;
  active_transfer: TransferProgress;
  runs: TrainingRunStatus[];
}

/** Payload of /innate_training/job_statuses. */
interface TrainingJobList {
  stamp: RosTime;
  skills: TrainingSkillStatus[];
}

/** create_run request params (preset + KEY=VALUE env overrides). */
interface TrainingParams {
  preset: string;
  env: string[];
  extra_json: string;
}

/** A skill from /brain/available_skills. Physical skills carry a directory. */
interface Skill {
  id: string;
  name: string;
  type: "code" | "learned" | "replay" | "poses" | "eval";
  /** Folder path inside the skill package ("" = root, e.g. "chess"). */
  group?: string;
  episode_count: number;
  /** Dataset directory on the robot; absent for code-only skills. */
  directory?: string;
  in_training?: boolean;
}

/** One recorded episode, as returned by the get_task_metadata SERVICE (note:
 * the recorder reshapes the on-disk file — episode_id is "episode_N", and the
 * fields are start_time/end_time/num_timesteps). */
interface EpisodeSummary {
  episode_id: string; // e.g. "episode_0"
  start_time: string; // ISO datetime
  end_time: string; // ISO datetime
  num_timesteps: number;
  file_name: string; // e.g. "episode_0.h5"
  /** Present once converted to H.264, one MP4 per camera. */
  video_files?: string[];
  /** Curation label; "" / absent = unlabeled (treated as success by default). */
  outcome?: "success" | "failure" | "";
  /** Provenance: how the episode was produced. Legacy episodes default to
   * "teleop" (the only pre-provenance recording path). */
  source?: "teleop" | "rollout" | "replay";
  /** For rollouts, the id of the policy/skill that drove the run; "" if n/a. */
  policy?: string;
  /** Failure-mode tags set at eval review time (e.g. "dropped", "timeout"). */
  tags?: string[];
}

/** Decoded payload of get_task_metadata's json_metadata field. */
interface TaskSummary {
  task_name?: string;
  task_directory?: string;
  data_frequency: number; // Hz
  dataset_type?: "h5" | "h264";
  number_of_episodes?: number;
  episodes: EpisodeSummary[];
}

/** /datasets/encode_status — background encoder progress. */
interface EncodeStatusMsg {
  task_directory: string;
  stage: "idle" | "encoding" | "done" | "error" | "gated";
  current_file: string;
  current_index: number;
  total: number;
  progress: number; // 0..1
  message: string;
  error: string;
  queue_depth: number;
}

/** Decoded /episode/joints response. */
interface EpisodeJoints {
  qpos: number[][];
  qvel: number[][];
  timestamps: number[];
  data_frequency: number;
}

/** Who is currently driving. Pointer (joystick) always wins over keyboard. */
type DriveSource = "joystick" | "keyboard";

/**
 * The arbitrated drive vector, robot frame: x = steer (right positive),
 * y = throttle (forward positive), both in [-1, 1].
 */
interface DriveState {
  source: DriveSource | null;
  x: number;
  y: number;
  engaged: boolean;
}

/** Snapshot emitted by the keyboard drive for the WASD hint chips. */
interface KeyboardDriveState {
  held: { up: boolean; down: boolean; left: boolean; right: boolean };
  x: number;
  y: number;
  engaged: boolean;
}

/** geometry_msgs/Vector3 subset published to /joystick (z is implied 0). */
interface JoystickMsg {
  x: number;
  y: number;
}

/** std_msgs/String. */
interface StringMsg {
  data: string;
}

/** std_msgs/Int32 (head tilt degrees). */
interface Int32Msg {
  data: number;
}

/** sensor_msgs/BatteryState subset (robot publishes percentage as 0-100). */
interface BatteryStateMsg {
  percentage?: number;
  voltage?: number;
  current?: number;
}

/** JSON payload carried inside /robot/info's std_msgs/String. */
interface RobotInfo {
  robot_name?: string;
  version?: string;
  hostname?: string;
  hardware_revision?: string;
  supports_webrtc_audio?: boolean;
  /** Newer robot software with the recorder/replay services — gates the
   * Collect page's "Recorded movement" (replay skill) option. */
  supports_digital_skills?: boolean;
  volume_percent?: number; // 0–100 robot speaker volume
  /** Live manual-drive speed multiplier (0–1] on the motion_control caps. Absent
   * on robot software without speed modes. */
  drive_speed_scale?: number;
  /** The presets the robot offers, so clients don't each carry their own copy.
   * Absent on robot software predating it; clients fall back to a built-in table. */
  drive_speed_modes?: { id: string; label: string; scale: number }[];
}

/** JSON payload carried inside /mars/head/current_position's std_msgs/String. */
interface HeadPosition {
  current_position: number;
  min_angle?: number;
  max_angle?: number;
  default_angle?: number;
}

/** Snapshot emitted by DynamixelLeader on every change. */
interface LeaderArmState {
  /** Serial port open and the read loop running. */
  connected: boolean;
  /** Latest raw servo ticks (signed, 2048 = center), ordered by servo id. */
  positions: number[] | null;
  /** Position rounds per second over the last second. */
  rate: number;
  error: string | null;
}

// ---- WebSerial (Chromium-only; not in TypeScript's dom lib) ----------------

interface SerialPortInfo {
  usbVendorId?: number;
  usbProductId?: number;
}

interface SerialPort extends EventTarget {
  readonly readable: ReadableStream<Uint8Array> | null;
  readonly writable: WritableStream<Uint8Array> | null;
  open(options: { baudRate: number; bufferSize?: number }): Promise<void>;
  close(): Promise<void>;
  getInfo(): SerialPortInfo;
}

interface Serial extends EventTarget {
  getPorts(): Promise<SerialPort[]>;
  requestPort(options?: object): Promise<SerialPort>;
}

interface Navigator {
  readonly serial?: Serial;
}

/** Video link lifecycle. "streaming" means a live main-camera track is up. */
type WebRtcStatus = "idle" | "connecting" | "streaming" | "error";

/** Snapshot emitted by WebRtcSession on every change. */
interface WebRtcState {
  status: WebRtcStatus;
  /** The primary (big) camera's stream — what the full-bleed stage shows. */
  videoStream: MediaStream | null;
  /** Every negotiated camera's stream, indexed by m-line (null until a track arrives). */
  videoStreams: (MediaStream | null)[];
  /** Per m-line liveness: true once that camera's track is unmuted (real frames flowing). */
  videoLive: boolean[];
  audioStream: MediaStream | null;
  audioRequested: boolean;
  /** Live RTCPeerConnection.iceConnectionState ("new" before a peer exists). */
  iceState: string;
  /** True once we've fallen back to a public STUN server after the local-only config failed. */
  stunFallback: boolean;
}

type GazeStatus =
  | "off"
  | "paused"
  | "starting"
  | "error"
  | "searching"
  | "following"
  | "too_far"
  | "centering"
  | "locked";

interface GazeBox {
  center_x: number;
  center_y: number;
  width: number;
  height: number;
}

interface GazeKeypoint {
  name: string;
  x: number;
  y: number;
  confidence: number;
}

interface GazePerson {
  confidence: number;
  target: boolean;
  head_visible: boolean;
  body: GazeBox;
  head: GazeBox;
  keypoints: GazeKeypoint[];
}

interface GazeDetector {
  model: string;
  runtime: string;
  input_size: number;
  inference_ms: number;
  error: string;
}

interface GazeDebug {
  status: GazeStatus;
  progress: number;
  detector?: GazeDetector;
  faces: GazeBox[];
  target: GazeBox | null;
  people?: GazePerson[];
  zone: { left: number; top: number; right: number; bottom: number };
  image: { width: number; height: number };
  frame: number;
}

interface GazeOverlaySession {
  readonly primaryCamera: string | { name: string };
  onChange(callback: (state: unknown) => void): () => void;
}
