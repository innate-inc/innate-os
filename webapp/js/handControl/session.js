// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Camera arm control: your webcam drives the MARS claw.
//
// Everything here runs in the operator's browser -- frames never leave it. What
// reaches the robot is the same joint stream the Arm SDK page's sliders use
// (/armsdk/stream_joints -> Manipulation.stream_joints), so the arm server's
// velocity clamp, its 0.4 s stream idle-out and its motion lock are what bound
// this feature; there is no camera-specific path into the arm and no new robot
// service to own. Holding is therefore the absence of commands: stop publishing
// and the stream idles out with the arm where it stands. Nothing is ever queued
// -- a frame either goes out now or is dropped.
//
// The mapping (hand -> position, tilt, pinch) is the studio's, measured against
// a MuJoCo MARS in sim/hand_control; this module adds the robot side of it:
// anchoring on the arm's *achieved* pose, closed-form IK (armKinematics.js),
// and the holds that make losing your hand or the network safe.

import { ARM_STATUS_TOPIC } from "../constants.js";
import { measureHand, continuousHand, Calibration, HandMapper, clamp } from "./handSample.js";
import { WristMapper, WRIST_LIMITS } from "./orientation.js";
import { forwardArm, solveArm, SHOULDER, GRIPPER_OPEN } from "./armKinematics.js";

const STREAM_TOPIC = "/armsdk/stream_joints";
const STREAM_TYPE = "std_msgs/msg/Float64MultiArray";
const ARM_STATE_TOPIC = "/mars/arm/state";
const ARM_STATE_THROTTLE_MS = 50;

// The box the hand maps onto, in the arm's own frame with the base swivel taken
// out: reach, sideways, height around a comfortable mid-work pose. Sized by
// sweeping the solver -- around 80% of it holds any tilt from 17 degrees up to
// 69 degrees down exactly, and its floor is low enough to take something off
// the ground. A larger box would mostly add corners where the claw has to give
// up the tilt it was asked for.
const CENTER = [SHOULDER[0] + 0.26, SHOULDER[1], 0.105];
const SPAN = [0.04, 0.1, 0.095];
// Wider than the box, so the solver's radial search has somewhere to go.
/** @type {[number, number]} */
const RADIUS_BOUNDS = [0.16, 0.33];

const MAX_EE_SPEED = 0.18; // m/s, matching the studio's measured-safe rate
// How far the commanded point may run ahead of where the arm actually is. A
// stalled or slow arm therefore cannot bank up travel that it then lunges
// through, and starting from a pose outside the box simply walks in at the
// arm's own pace.
const MAX_LEAD_M = 0.08;
// Reanchoring reads the arm's *achieved* pose so control resumes from reality
// rather than from a turn that never finished. Within this much of what the arm
// was last told, though, the difference is servo droop under load, not a change
// of mind -- re-reading that as the new request would walk the claw a little
// further down and out on every hold, recentre and blink.
const RESUME_TOLERANCE_M = 0.03;
const RESUME_TOLERANCE_RAD = 0.35;

const PUBLISH_MIN_GAP_MS = 45;
const FRESH_RESULT_MS = 300; // hold if tracking goes quiet for longer
const STALE_FRAME_MS = 400; // a result about a frame this old is not control input
const RESUME_GRACE_MS = 600; // a blink keeps the original hand-to-arm mapping
const REACQUIRE_MS = 240;

const CAPTURE_MIN_GAP_MS = 31;
const TRACKER_TIMEOUT_MS = 30_000;

/** Roll fades out as the claw nears the floor: a rolled jaw digs in there. */
const rotationClearance = (/** @type {number} */ z) => {
  const t = clamp(z / 0.06, 0, 1);
  return t * t * (3 - 2 * t);
};

/** @param {[number, number, number]} point @param {number} yaw */
function swivel(point, yaw) {
  const x = point[0] - SHOULDER[0];
  const y = point[1] - SHOULDER[1];
  return /** @type {[number, number, number]} */ ([
    SHOULDER[0] + Math.cos(yaw) * x - Math.sin(yaw) * y,
    SHOULDER[1] + Math.sin(yaw) * x + Math.cos(yaw) * y,
    point[2],
  ]);
}

/**
 * @typedef {{ phase: "off" | "loading" | "ready" | "following" | "holding",
 *   feedback: string, error: string, progress: number, rate: number, inferenceMs: number,
 *   cameraOn: boolean, handTracked: boolean, connected: boolean, torque: boolean | null,
 *   grip: number, limited: boolean, approaching: boolean, heightMm: number | null,
 *   blockedBy: string | null }} HandControlState
 */

/**
 * @param {object} options
 * @param {import("../rosClient.js").RosClient} options.ros
 * @param {HTMLVideoElement} options.video the preview the operator sees
 * @param {(sample: import("./handSample.js").HandSample | null) => void} options.onSample
 *   per-result hook for the landmark overlay
 * @param {(state: HandControlState) => void} options.onState
 * @param {() => (() => void) | null} options.claim in-page interlock against other arm control
 * @param {() => string | null} options.blockedBy names whoever else holds the arm, for the UI
 */
export function createHandControl({ ros, video, onSample, onState, claim, blockedBy }) {
  const mapper = new HandMapper();
  const wrist = new WristMapper();
  const reacquire = new Calibration(REACQUIRE_MS, 4);

  /** @type {"off" | "loading" | "ready" | "following" | "holding"} */
  let phase = "off";
  let feedback = "Your video stays in this browser.";
  let error = "";
  let progress = 0;
  let destroyed = false;

  /** @type {MediaStream | null} */ let stream = null;
  /** @type {Worker | null} */ let worker = null;
  let trackerReady = false;
  let busy = false;
  let cameraGeneration = 0;
  let lastVideoTime = -1;
  let lastCaptureAt = 0;

  /** @type {import("./handSample.js").HandSample | null} */ let sample = null;
  /** @type {import("./handSample.js").HandSample | null} */ let previousHand = null;
  let lastResultAt = 0;
  let heldSince = 0;
  /** @type {number[]} */ let frameTimes = [];
  /** @type {number[]} */ let inferenceTimes = [];
  let rate = 0;
  let inferenceMs = 0;

  /** Measured arm, straight off the driver topics. @type {number[] | null} */
  let measured = null;
  /** @type {boolean | null} */ let torque = null;
  /** @type {[number, number, number] | null} */ let pathTarget = null;
  /** @type {number[]} */ let command = [0, 0, 0, 0, 0, GRIPPER_OPEN];
  /** The last pose actually asked for, or null before this session commanded one. */
  /** @type {{ x: number, y: number, z: number, radius: number, roll: number, pitch: number, yaw: number } | null} */
  let commandedPose = null;
  let pitchTarget = 0;
  let gripTarget = 1;
  let limited = false;
  let lastPublishAt = 0;
  let lastTickAt = 0;
  /** @type {(() => void) | null} */ let release = null;
  /** @type {(() => void) | null} */ let unadvertise = null;

  const following = () => phase === "following" || phase === "holding";
  /** @param {number[]} a @param {number[]} b */
  const distance = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);

  function publishState() {
    if (destroyed) return;
    const fresh = !!sample?.valid && performance.now() - lastResultAt < 500;
    const pose = measured ? forwardArm(measured) : null;
    onState({
      phase,
      feedback,
      error,
      progress,
      rate,
      inferenceMs,
      cameraOn: !!stream,
      handTracked: fresh,
      connected: ros.state === "connected",
      torque,
      grip: measured ? clamp(measured[5] / GRIPPER_OPEN, 0, 1) : gripTarget,
      limited,
      approaching: !!pathTarget && !!pose && distance(pathTarget, [pose.x, pose.y, pose.z]) > 0.02,
      heightMm: pose ? Math.max(0, pose.z) * 1000 : null,
      blockedBy: blockedBy(),
    });
  }

  // ---- control ------------------------------------------------------------

  /** The arm's own pose as a starting point for the hand: normalized box offset,
   * claw angles and grip, all from measured joints so nothing jumps on start. */
  function anchorFromArm() {
    const achieved = forwardArm(/** @type {number[]} */ (measured));
    const asked = commandedPose;
    const settled =
      !!asked &&
      distance([asked.x, asked.y, asked.z], [achieved.x, achieved.y, achieved.z]) < RESUME_TOLERANCE_M &&
      Math.abs(asked.pitch - achieved.pitch) < RESUME_TOLERANCE_RAD &&
      Math.abs(asked.roll - achieved.roll) < RESUME_TOLERANCE_RAD;
    const pose = settled && asked ? asked : achieved;
    const clearance = rotationClearance(pose.z);
    /** @type {[number, number, number]} */
    const offset = [
      0, // sideways is relative to the arm's own plane, so it starts centred
      clamp((pose.z - CENTER[2]) / SPAN[2]),
      clamp((SHOULDER[0] + pose.radius - CENTER[0]) / SPAN[0]),
    ];
    /** @type {[number, number, number]} */
    const angles = [
      clearance > 0.01 ? clamp(pose.roll / clearance, WRIST_LIMITS[0][0], WRIST_LIMITS[0][1]) : 0,
      clamp(pose.pitch, WRIST_LIMITS[1][0], WRIST_LIMITS[1][1]),
      clamp(pose.yaw, WRIST_LIMITS[2][0], WRIST_LIMITS[2][1]),
    ];
    // A gripped claw stalls short of its target and that error is the grip
    // force, so the standing grip -- not the measured jaw -- is the anchor.
    const grip = settled ? gripTarget : clamp(/** @type {number[]} */ (measured)[5] / GRIPPER_OPEN, 0, 1);
    return { offset, angles, pose, grip };
  }

  /** @param {import("./handSample.js").HandSample} hand */
  function anchorAt(hand) {
    const { offset, angles, pose, grip } = anchorFromArm();
    mapper.anchor(hand, offset, grip);
    wrist.anchor(hand.orientation ?? null, angles);
    pitchTarget = angles[1];
    gripTarget = grip;
    pathTarget = [pose.x, pose.y, pose.z];
    command = [.../** @type {number[]} */ (measured).slice(0, 5), grip * GRIPPER_OPEN];
    commandedPose = null;
    previousHand = hand;
    lastTickAt = performance.now();
    reacquire.reset();
  }

  /** Stop feeding the stream. The arm server idles the stream out and the arm
   * holds where it is -- there is nothing to flush and nothing queued.
   * @param {string} reason */
  function hold(reason) {
    if (phase !== "holding") heldSince = performance.now();
    phase = "holding";
    feedback = reason;
    progress = 0;
    reacquire.reset();
    publishState();
  }

  /** @param {number} now @param {number} capturedAge */
  function drive(now, capturedAge) {
    const hand = sample;
    if (!hand?.valid) {
      hold(hand?.reason || "Bring your hand back into view");
      return;
    }
    if (capturedAge > STALE_FRAME_MS) {
      hold("Camera is catching up · your arm holds here");
      return;
    }
    if (ros.state !== "connected") {
      hold("Robot connection lost · your arm holds here");
      return;
    }
    if (phase === "following" && !continuousHand(previousHand, hand)) {
      hold("Hand moved out of view. Hold briefly to resume here.");
    }
    if (phase === "holding") {
      // A blink keeps the original hand-to-arm mapping; a longer absence
      // re-anchors on the arm's achieved pose so nothing jumps on resume.
      if (now - heldSince < RESUME_GRACE_MS && continuousHand(previousHand, hand)) {
        phase = "following";
        lastTickAt = now;
      } else {
        const held = reacquire.update(hand, now);
        progress = held.progress;
        feedback = "Hold still briefly to resume from here";
        if (!held.ready) return;
        anchorAt(hand);
        phase = "following";
      }
    }

    const value = mapper.map(hand, now);
    if (!value) return;
    const angles = wrist.update(hand.orientation ?? null, now);
    const dt = clamp((now - lastTickAt) / 1000, 0.001, 0.2);
    lastTickAt = now;

    /** @type {[number, number, number]} */
    const desired = swivel(
      [
        CENTER[0] + SPAN[0] * value.position[2],
        CENTER[1] + SPAN[1] * value.position[0],
        CENTER[2] + SPAN[2] * value.position[1],
      ],
      angles[2],
    );

    const pose = forwardArm(/** @type {number[]} */ (measured));
    let target = pathTarget ?? [pose.x, pose.y, pose.z];
    const gap = distance(desired, target);
    const step = Math.min(1, (MAX_EE_SPEED * dt) / Math.max(gap, 1e-9));
    target = /** @type {[number, number, number]} */ (target.map((v, i) => v + (desired[i] - v) * step));
    const lead = distance(target, [pose.x, pose.y, pose.z]);
    if (lead > MAX_LEAD_M) {
      const k = MAX_LEAD_M / lead;
      target = /** @type {[number, number, number]} */ ([pose.x, pose.y, pose.z].map((v, i) => v + (target[i] - v) * k));
    }
    pathTarget = target;

    pitchTarget = angles[1];
    const roll = angles[0] * rotationClearance(target[2]);
    const solved = solveArm(target, pitchTarget, roll, command, { radius: RADIUS_BOUNDS });
    limited = solved.limited;
    gripTarget = value.grip;
    command = [...solved.joints, clamp(value.grip, 0, 1) * GRIPPER_OPEN];
    commandedPose = forwardArm(command);

    if (now - lastPublishAt < PUBLISH_MIN_GAP_MS) return;
    lastPublishAt = now;
    if (!ros.publish(STREAM_TOPIC, { layout: { dim: [], data_offset: 0 }, data: command })) {
      hold("Waiting for the robot connection…");
      return;
    }
    previousHand = hand;
    feedback = limited
      ? "At the edge of the arm's reach — move back toward where you started"
      : !hand.orientation
        ? "Finding your finger orientation · claw rotation holds"
        : !hand.gripValid
          ? "Thumb or index out of view · the gripper holds"
          : Math.max(...value.position.map(Math.abs)) > 0.97
            ? "At the edge of your working area — move back toward centre"
            : "Move, turn and tilt. Pinch to grip.";
  }

  // ---- tracker ------------------------------------------------------------

  /** @param {MessageEvent<any>} event */
  function onResult({ data }) {
    busy = false;
    if (data.type === "error") {
      error = "Hand tracking stopped. Enable the camera again to restart it.";
      disableCamera();
      return;
    }
    if (data.type !== "result" || data.generation !== cameraGeneration || !stream) return;
    const now = performance.now();
    lastResultAt = now;
    frameTimes = frameTimes.filter((t) => now - t < 1000);
    frameTimes.push(now);
    rate = frameTimes.length;
    inferenceTimes.push(data.inferenceMs);
    if (inferenceTimes.length > 30) inferenceTimes.shift();
    inferenceMs = inferenceTimes.reduce((a, b) => a + b, 0) / inferenceTimes.length;

    sample = measureHand(data.result, video.videoWidth, video.videoHeight);
    onSample(sample);
    if (following() && !measured) hold("Waiting for the arm's joint state…");
    else if (following()) drive(now, now - data.timestamp);
    else if (phase === "ready") feedback = sample.valid ? "Ready — start following when you are." : sample.reason;
    publishState();
  }

  async function startTracker() {
    if (trackerReady) return;
    worker?.terminate();
    const spawned = new Worker("/js/handControl/tracker.worker.js");
    worker = spawned;
    await new Promise((resolve, reject) => {
      const timer = setTimeout(
        () => reject(new Error("The hand tracker took too long to load. Try enabling the camera again.")),
        TRACKER_TIMEOUT_MS,
      );
      spawned.onerror = (event) => {
        clearTimeout(timer);
        reject(new Error(event.message || "Unable to start the hand tracker."));
      };
      spawned.onmessage = ({ data }) => {
        if (data.type === "ready") {
          clearTimeout(timer);
          trackerReady = true;
          resolve(undefined);
        }
        if (data.type === "error") {
          clearTimeout(timer);
          reject(new Error(data.message));
        }
      };
      spawned.postMessage({ type: "init" });
    });
    spawned.onerror = () => {
      error = "Hand tracking stopped. Enable the camera again to restart it.";
      disableCamera();
    };
    spawned.onmessage = onResult;
  }

  /** @param {number} now */
  async function capture(now) {
    if (!stream || destroyed) return;
    requestAnimationFrame(capture);
    if (
      !trackerReady ||
      busy ||
      video.readyState < 2 ||
      video.currentTime === lastVideoTime ||
      now - lastCaptureAt < CAPTURE_MIN_GAP_MS ||
      document.hidden
    )
      return;
    busy = true;
    lastVideoTime = video.currentTime;
    lastCaptureAt = now;
    const generation = cameraGeneration;
    try {
      const bitmap = await createImageBitmap(video);
      if (generation !== cameraGeneration || !stream || !worker) {
        bitmap.close();
        busy = false;
        return;
      }
      worker.postMessage({ type: "frame", bitmap, generation, timestamp: now }, [bitmap]);
    } catch {
      busy = false;
      if (following()) hold("Camera paused · your arm holds here");
    }
  }

  // ---- lifecycle ----------------------------------------------------------

  async function enableCamera() {
    if (phase === "loading" || stream) return;
    const generation = ++cameraGeneration;
    phase = "loading";
    error = "";
    feedback = "Allow camera access in your browser.";
    publishState();
    try {
      const obtained = await navigator.mediaDevices.getUserMedia({
        audio: false,
        video: { width: { ideal: 640 }, height: { ideal: 480 }, frameRate: { ideal: 30, max: 30 }, facingMode: "user" },
      });
      if (generation !== cameraGeneration || destroyed) {
        obtained.getTracks().forEach((t) => t.stop());
        return;
      }
      stream = obtained;
      video.srcObject = stream;
      await video.play();
      stream.getVideoTracks()[0].onended = () => {
        error = "Your camera disconnected. Reconnect it, then enable the camera again.";
        disableCamera();
      };
    } catch (err) {
      if (generation !== cameraGeneration) return;
      const name = err instanceof DOMException ? err.name : "";
      error =
        name === "NotAllowedError"
          ? "Camera permission was declined. Allow camera access for this page, then try again."
          : name === "NotFoundError"
            ? "No webcam found. Connect a camera, then try again."
            : `Couldn't open the camera. ${err instanceof Error ? err.message : err}`;
      disableCamera();
      return;
    }
    feedback = "Preparing hand tracking…";
    publishState();
    try {
      await startTracker();
    } catch (err) {
      if (generation === cameraGeneration) {
        error = err instanceof Error ? err.message : String(err);
        disableCamera();
      }
      return;
    }
    if (generation !== cameraGeneration || destroyed) return;
    phase = "ready";
    feedback = "Hold your hand comfortably in view, then start following.";
    lastVideoTime = -1;
    lastResultAt = performance.now();
    publishState();
    requestAnimationFrame(capture);
  }

  function disableCamera() {
    stop("Camera is off · your arm holds here");
    cameraGeneration++;
    stream?.getTracks().forEach((track) => {
      track.onended = null;
      track.stop();
    });
    stream = null;
    video.srcObject = null;
    worker?.terminate();
    worker = null;
    trackerReady = false;
    busy = false;
    sample = null;
    previousHand = null;
    onSample(null);
    phase = "off";
    progress = 0;
    frameTimes = [];
    inferenceTimes = [];
    rate = 0;
    inferenceMs = 0;
    publishState();
  }

  /** @returns {string} the reason it cannot start, or "" */
  function blocker() {
    if (!stream || !trackerReady) return "Enable your camera first.";
    if (ros.state !== "connected") return "Waiting for the robot connection.";
    if (!measured) return "Waiting for the arm's joint state.";
    if (torque === false) return "The arm is limp — turn torque on first.";
    if (!sample?.valid || performance.now() - lastResultAt > STALE_FRAME_MS)
      return sample?.reason || "Bring your hand into view.";
    const held = blockedBy();
    if (held) return `${held} is driving the arm — stop it first.`;
    return "";
  }

  function start() {
    const why = blocker();
    if (why) {
      feedback = why;
      publishState();
      return;
    }
    release ??= claim();
    if (!release) {
      feedback = `${blockedBy()} is driving the arm — stop it first.`;
      publishState();
      return;
    }
    // rws resolves a bare publish against the live graph, which can come up
    // empty right after a reconnect; declaring the type makes the stream land.
    unadvertise ??= ros.advertise(STREAM_TOPIC, STREAM_TYPE);
    error = "";
    limited = false;
    anchorAt(/** @type {import("./handSample.js").HandSample} */ (sample));
    phase = "following";
    feedback = "You're in control. Move slowly to explore.";
    publishState();
  }

  /** @param {string} [reason] */
  function stop(reason = "Stopped · your arm holds here") {
    if (following()) {
      phase = stream && trackerReady ? "ready" : "off";
      feedback = reason;
    }
    pathTarget = null;
    commandedPose = null;
    progress = 0;
    limited = false;
    reacquire.reset();
    unadvertise?.();
    unadvertise = null;
    release?.();
    release = null;
    publishState();
  }

  function recenter() {
    if (!stream || !trackerReady || !measured) return;
    if (!sample?.valid) {
      hold("Bring your hand into view to recentre");
      return;
    }
    anchorAt(sample);
    if (following()) {
      phase = "following";
      feedback = "Recentred here. Carry on.";
    }
    publishState();
  }

  // ---- robot feeds --------------------------------------------------------

  const unsubs = [
    ros.subscribe(
      ARM_STATE_TOPIC,
      (/** @type {any} */ msg) => {
        if (!Array.isArray(msg?.position) || msg.position.length < 6) return;
        measured = msg.position.slice(0, 6).map(Number);
      },
      ARM_STATE_THROTTLE_MS,
      "sensor_msgs/msg/JointState",
    ),
    ros.subscribe(
      ARM_STATUS_TOPIC,
      (/** @type {any} */ msg) => {
        if (typeof msg?.is_torque_enabled !== "boolean") return;
        torque = msg.is_torque_enabled;
        if (torque === false && following()) stop("The arm went limp · control stopped");
        else publishState();
      },
      undefined,
      "mars_msgs/msg/ArmStatus",
    ),
    ros.onStateChange((state) => {
      if (state !== "connected" && following()) stop("Robot connection lost · control stopped");
      else publishState();
    }),
  ];

  // Tracking can stop producing results without any error (a stalled camera, a
  // throttled tab): hold on silence rather than on a failure that never comes.
  const watchdog = setInterval(() => {
    if (phase === "following" && performance.now() - lastResultAt > FRESH_RESULT_MS)
      hold("Camera catching up · your arm holds here");
  }, 100);

  const onVisibility = () => {
    if (document.hidden && following()) stop("Paused while this tab was away");
  };
  document.addEventListener("visibilitychange", onVisibility);

  publishState();

  return {
    enableCamera,
    disableCamera,
    start,
    stop,
    recenter,
    /** @param {number} value */
    setSensitivity(value) {
      mapper.sensitivity = value;
    },
    destroy() {
      destroyed = true;
      clearInterval(watchdog);
      document.removeEventListener("visibilitychange", onVisibility);
      for (const unsub of unsubs) unsub();
      disableCamera();
    },
  };
}
