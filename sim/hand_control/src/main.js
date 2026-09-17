import "./style.css";
import {
  Calibration,
  GroundCalibration,
  HandMapper,
  measureHand,
  continuousHand,
} from "./control.mjs";
import { WristMapper } from "./orientation.mjs";
import { PersonalMapper, personalSample } from "./personal.mjs";
import { PinchMapper, pinchSample } from "./pinch.mjs";
import { canAlignJawRoll } from "./fingertip-rotation.mjs";
import { createScene } from "./scene.js";

const $ = (id) => document.getElementById(id);
const video = $("camera"),
  overlay = $("landmarks"),
  context = overlay.getContext("2d");
let calibration = new GroundCalibration(),
  personal = null,
  profile = null;
const reacquire = new Calibration(240, 4),
  mapper = new HandMapper(),
  wristMapper = new WristMapper();
let scene,
  ws,
  connected = false,
  sceneReady = false,
  socketGeneration = 0;
let stream = null,
  worker = null,
  trackerReady = false,
  busy = false,
  cameraGeneration = 0;
let phase = "off",
  calibrated = false,
  alignRollPending = true,
  armed = false,
  token = null,
  pendingBegin = 0,
  sequence = 0;
let lastSample = null,
  lastResultAt = 0,
  lastStateAt = 0,
  lastVideoTime = -1,
  lastSent = 0,
  lastHeartbeat = 0,
  heldSince = 0,
  previousHand = null,
  beginPending = false;
let state = { offset: [0, 0, 0], grip: 1 },
  timestamps = [],
  inferenceTimes = [],
  rate = 0,
  inferenceMs = 0;
let errorText = "",
  feedback = "Your video stays in this browser.",
  progress = 0;
const FINGERS = [
  [0, 1, 2, 3, 4],
  [0, 5, 6, 7, 8],
  [0, 9, 10, 11, 12],
  [0, 13, 14, 15, 16],
  [0, 17, 18, 19, 20],
];
const CAMERA_ICON =
  '<svg viewBox="0 0 24 24"><rect x="3" y="6" width="12" height="12" rx="3"/><path d="m15 10 6-3v10l-6-3"/></svg>';
const PLAY_ICON = '<svg viewBox="0 0 24 24"><path d="m9 5 11 7-11 7Z"/></svg>';
const PAUSE_ICON = '<svg viewBox="0 0 24 24"><path d="M9 5v14M15 5v14"/></svg>';
const ROLL_ALIGNMENT_HINT =
  "Separate your thumb and index and turn them slightly so the jaw line is visible.";

function rollReady(sample) {
  return (
    !personal?.rotation ||
    !alignRollPending ||
    canAlignJawRoll(sample, state.wrist)
  );
}

function send(message) {
  if (ws?.readyState === WebSocket.OPEN && ws.bufferedAmount < 4096) {
    ws.send(JSON.stringify(message));
    return true;
  }
  return false;
}

function stopLease() {
  pendingBegin++;
  beginPending = false;
  token = null;
  send({ op: "stop" });
}

function pause(reason = "Paused · your arm holds here") {
  armed = false;
  stopLease();
  reacquire.reset();
  progress = 0;
  if (stream && calibrated) phase = "ready";
  feedback = reason;
  render();
}

function holdTracking(reason) {
  if (phase !== "holding") {
    heldSince = performance.now();
    if (token) send({ op: "hold", token });
  }
  phase = "holding";
  feedback = reason;
  progress = 0;
  reacquire.reset();
  render();
}

function begin() {
  if (
    beginPending ||
    !connected ||
    !sceneReady ||
    !lastSample?.valid ||
    performance.now() - lastResultAt > 400
  )
    return;
  if (!rollReady(lastSample)) {
    feedback = ROLL_ALIGNMENT_HINT;
    render();
    return;
  }
  const request = ++pendingBegin;
  beginPending = true;
  phase = "holding";
  heldSince = performance.now();
  feedback = "Connecting your hand to MARS…";
  if (!send({ op: "begin", request, controller: "innate-ik-camera-v1" })) {
    beginPending = false;
    feedback = "Waiting for the connection…";
  }
}

function follow() {
  if (!calibrated || !connected || !sceneReady) return;
  errorText = "";
  armed = true;
  if (lastSample?.valid && performance.now() - lastResultAt < 400) begin();
  else holdTracking("Bring your hand into view to begin");
  render();
}

// Fitted profiles carry a revision; a hand-written one is still a profile.
const profileIdentity = (p) =>
  p ? p.revision || p.source_hash || JSON.stringify(p) : null;

function connect() {
  const generation = ++socketGeneration;
  const socket = new WebSocket(
    `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/control`,
  );
  ws = socket;
  socket.onmessage = ({ data }) => {
    if (generation !== socketGeneration) return;
    const message = JSON.parse(data);
    if (message.type === "hello") {
      if (message.simulated !== true) {
        socket.close();
        errorText = "This workspace requires the local simulator.";
        render();
        return;
      }
      connected = true;
      if (
        profileIdentity(message.personal_profile) !== profileIdentity(profile)
      ) {
        profile = message.personal_profile || null;
        personal = profile?.pinch
          ? new PinchMapper(profile)
          : profile
            ? new PersonalMapper(profile)
            : null;
        if (personal) personal.sensitivity = mapper.sensitivity;
        calibration = personal ? new Calibration() : new GroundCalibration();
        calibrated = false;
        alignRollPending = true;
        mapper.heightRange = null;
        if (stream) phase = "calibrating";
        $("personal-status").hidden = !personal;
        $("personal-status").textContent =
          profile?.pinch?.rotation === "direct"
            ? "Pinch center · fingertip pose control"
            : profile?.pinch
              ? `Tuned from your ${profile.pinch.pose_count} poses · thumb + index control`
              : profile?.floor
                ? "Tuned from your 23 poses · including floor grasps"
                : "Tuned from your 16 poses · recenter anywhere";
        $("movement-detail").textContent = personal
          ? "Start comfortably. Move your hand to move the claw."
          : "The bottom line is ground. Lift to raise.";
        $("rotation-note").textContent = personal?.rotation
          ? "Twist to roll the claw; tilt your fingers to aim. The point between them stays your grasp point."
          : personal
            ? "Turn your hand to swivel the arm. Tilt to aim the claw; pinch thumb + index to grasp. Recenter for a fresh starting position."
            : "Point and tilt your thumb + index to aim the claw. Yaw swivels the whole arm.";
      }
      lastStateAt = performance.now();
      render();
    }
    if (message.type === "state") {
      state = message;
      lastStateAt = performance.now();
      scene?.update(state);
      if (state.ik_error && armed) {
        errorText =
          "The arm solver is unavailable. Restart the studio to reconnect.";
        pause("Arm held safely");
      }
    }
    if (message.type === "begun") {
      // A delayed acknowledgment must not cancel a more recent start request.
      if (message.request !== pendingBegin) return;
      beginPending = false;
      if (
        !armed ||
        !lastSample?.valid ||
        performance.now() - lastResultAt > 400
      ) {
        stopLease();
        return;
      }
      token = message.token;
      sequence = 0;
      mapper.anchor(lastSample, message.offset, state.grip);
      wristMapper.anchor(lastSample.orientation, state.wrist);
      if (
        personal &&
        !personal.anchor(lastSample, message.offset, state.grip, state.wrist, {
          alignRoll: alignRollPending,
        })
      ) {
        stopLease();
        holdTracking(
          alignRollPending
            ? ROLL_ALIGNMENT_HINT
            : "Hold your relaxed hand in view to begin",
        );
        return;
      }
      previousHand = lastSample;
      alignRollPending = false;
      phase = "following";
      feedback = "You’re in control. Move slowly to explore.";
      render();
    }
    if (message.type === "held" && armed)
      holdTracking("Waiting for a fresh camera frame…");
    if (message.type === "error") {
      errorText = message.message;
      pause("Control paused");
    }
  };
  socket.onclose = () => {
    if (generation !== socketGeneration) return;
    connected = false;
    pause("Reconnecting to the simulation…");
    setTimeout(connect, 1200);
  };
  socket.onerror = () => {
    connected = false;
    render();
  };
}

async function initTracker() {
  if (trackerReady) return;
  worker?.terminate();
  // MediaPipe's WASM loader uses importScripts. A bundled classic worker
  // supports that directly and keeps synchronous inference off the UI thread.
  worker = new Worker(new URL("./tracker.worker.js", import.meta.url));
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(
      () =>
        reject(
          new Error(
            "The hand tracker took too long to load. Try enabling the camera again.",
          ),
        ),
      30000,
    );
    worker.onerror = (event) => {
      clearTimeout(timeout);
      reject(new Error(event.message || "Unable to start the hand tracker."));
    };
    worker.onmessage = ({ data }) => {
      if (data.type === "ready") {
        clearTimeout(timeout);
        trackerReady = true;
        resolve();
      }
      if (data.type === "error") {
        clearTimeout(timeout);
        reject(new Error(data.message));
      }
    };
    worker.postMessage({
      type: "init",
      base: new URL("./", location.href).href,
    });
  });
  worker.onerror = () => {
    errorText =
      "The hand tracker stopped. Enable the camera again to restart it.";
    disableCamera();
  };
  worker.onmessage = ({ data }) => {
    busy = false;
    if (data.type === "error") {
      errorText = "Tracking stopped. Enable the camera again to restart it.";
      disableCamera();
      return;
    }
    if (
      data.type !== "result" ||
      data.generation !== cameraGeneration ||
      !stream
    )
      return;
    const now = performance.now();
    if (now - data.timestamp > 400) {
      if (armed) holdTracking("Camera is catching up. Hold steady.");
      return;
    }
    lastResultAt = now;
    timestamps.push(now);
    timestamps = timestamps.filter((t) => now - t < 1000);
    rate = timestamps.length;
    inferenceTimes.push(data.inferenceMs);
    if (inferenceTimes.length > 30) inferenceTimes.shift();
    inferenceMs =
      inferenceTimes.reduce((a, b) => a + b, 0) / inferenceTimes.length;
    const sample = (
      profile?.pinch ? pinchSample : personal ? personalSample : measureHand
    )(data.result, video.videoWidth, video.videoHeight);
    lastSample = sample;
    draw(sample);
    if (!calibrated) {
      const calibrationSample =
        sample.valid && !rollReady(sample)
          ? { ...sample, valid: false, reason: ROLL_ALIGNMENT_HINT }
          : sample;
      const result = calibration.update(calibrationSample, now);
      progress = result.progress;
      feedback = calibrationSample.valid
        ? personal?.rotation
          ? "Separate your thumb and index slightly. Hold still to align the jaws."
          : personal
            ? "Hold your relaxed hand here for a moment."
            : result.reason
        : calibrationSample.reason;
      if (result.ready) {
        calibrated = true;
        if (!personal) mapper.setGround(result.bounds);
        phase = "ready";
        feedback = personal?.rotation
          ? "Start following to align the jaws with your fingers."
          : personal
            ? "Starting pose set. Start following to try your taught gestures."
            : "Ground set. Raise your hand to lift the arm.";
      }
    } else if (armed) {
      if (!sample.valid) {
        holdTracking(sample.reason);
      } else if (!token) {
        begin();
      } else {
        if (phase === "following" && !continuousHand(previousHand, sample))
          holdTracking("Hand moved out of view. Hold briefly to resume here.");
        if (phase === "holding") {
          // Small tracking blips preserve the original hand-to-arm mapping.
          // After a longer absence, anchor to the held pose to prevent a jump.
          if (now - heldSince < 600 && continuousHand(previousHand, sample)) {
            phase = "following";
          } else {
            const result = reacquire.update(sample, now);
            progress = result.progress;
            feedback = "Hold briefly to resume from this position";
            if (result.ready) {
              mapper.anchor(sample, state.offset, state.grip);
              wristMapper.anchor(sample.orientation, state.wrist);
              personal?.anchor(sample, state.offset, state.grip, state.wrist);
              phase = "following";
              reacquire.reset();
            }
          }
        }
        if (phase === "following") {
          const value = personal
            ? personal.map(sample, now)
            : mapper.map(sample, now);
          if (value) {
            const ok = send({
              op: "move",
              token,
              sequence: ++sequence,
              captured_at: data.capturedAt,
              horizontal: value.position[0],
              vertical: value.position[1],
              reach: value.position[2],
              grip: value.grip,
              wrist: value.wrist || wristMapper.update(sample.orientation, now),
            });
            if (!ok) holdTracking("Waiting for the connection to catch up…");
            else {
              previousHand = sample;
              feedback =
                state.rotation_limited || personal?.rotation?.limited
                  ? personal?.rotation
                    ? "Angle limited here. Move or tilt back to reach it."
                    : personal
                      ? "At the edge of this pose. Move a little toward your starting position."
                      : "At a joint limit. Tilt back the other way to move freely."
                  : personal?.rotation
                    ? "Twist and tilt to aim the claw. Pinch to grip."
                    : !sample.orientation
                      ? "Finding finger orientation · claw rotation holds"
                      : personal
                        ? "Your gestures are connected. Tilt, roll and pinch naturally."
                        : value.position[1] < -0.97
                          ? "At ground level. Lift your hand to raise the arm."
                          : Math.max(...value.position.map(Math.abs)) > 0.97
                            ? "At the edge of your reach. Move back toward center."
                            : !sample.gripValid
                              ? "Thumb or index fingertip out of view · gripper holds"
                              : "Point and tilt your thumb + index. Pinch to grip.";
            }
          } else if (personal)
            holdTracking("Bring your hand back toward its relaxed pose");
        }
      }
    } else if (sample.valid)
      feedback = "Ready. Start following, or recenter for a new position.";
    else feedback = sample.reason;
    render();
  };
}

function draw(sample) {
  overlay.width = video.videoWidth;
  overlay.height = video.videoHeight;
  context.clearRect(0, 0, overlay.width, overlay.height);
  if (!sample.raw) return;
  const points = sample.raw.map((p) => [
    p.x * overlay.width,
    p.y * overlay.height,
  ]);
  context.strokeStyle = sample.valid ? "#bfffe0" : "#f9deb0";
  context.lineWidth = 2;
  context.lineCap = "round";
  for (const finger of FINGERS) {
    context.beginPath();
    finger.forEach((i, n) =>
      n ? context.lineTo(...points[i]) : context.moveTo(...points[i]),
    );
    context.stroke();
  }
  for (const p of points) {
    context.beginPath();
    context.arc(...p, 3, 0, Math.PI * 2);
    context.fillStyle = "#fff";
    context.fill();
  }
  if (sample.gripValid) {
    context.save();
    context.strokeStyle = "#d4ff72";
    context.lineWidth = 3;
    context.setLineDash([5, 5]);
    context.beginPath();
    context.moveTo(...points[4]);
    context.lineTo(...points[8]);
    context.stroke();
    context.restore();
  }
  context.beginPath();
  context.arc(
    (1 - sample.x) * overlay.width,
    sample.y * overlay.height,
    11,
    0,
    Math.PI * 2,
  );
  context.stroke();
}

async function capture(now) {
  if (!stream) return;
  requestAnimationFrame(capture);
  if (
    !trackerReady ||
    busy ||
    video.readyState < 2 ||
    video.currentTime === lastVideoTime ||
    now - lastSent < 31 ||
    document.hidden
  )
    return;
  busy = true;
  lastVideoTime = video.currentTime;
  lastSent = now;
  const generation = cameraGeneration,
    capturedAt = Date.now();
  try {
    const bitmap = await createImageBitmap(video);
    if (generation !== cameraGeneration || !stream) {
      bitmap.close();
      busy = false;
      return;
    }
    worker.postMessage(
      { type: "frame", bitmap, generation, timestamp: now, capturedAt },
      [bitmap],
    );
  } catch {
    busy = false;
    if (armed) holdTracking("Camera paused. Hold steady.");
  }
}

async function enableCamera() {
  if (phase === "loading") return;
  const generation = ++cameraGeneration;
  phase = "loading";
  errorText = "";
  feedback = "Allow camera access in your browser.";
  render();
  let obtained;
  try {
    obtained = await navigator.mediaDevices.getUserMedia({
      audio: false,
      video: {
        width: { ideal: 640 },
        height: { ideal: 480 },
        frameRate: { ideal: 30, max: 30 },
        facingMode: "user",
      },
    });
    if (generation !== cameraGeneration) {
      obtained.getTracks().forEach((t) => t.stop());
      return;
    }
    stream = obtained;
    video.srcObject = stream;
    await video.play();
    stream.getVideoTracks()[0].onended = () => {
      errorText =
        "Your camera disconnected. Reconnect it, then enable the camera again.";
      disableCamera();
    };
    $("camera-placeholder").hidden = true;
  } catch (error) {
    if (generation !== cameraGeneration) return;
    errorText =
      error.name === "NotAllowedError"
        ? "Camera permission was declined. Allow camera access for this page, then try again."
        : error.name === "NotFoundError"
          ? "No webcam found. Connect a camera, then try again."
          : `Couldn’t open the camera. ${error.message}`;
    disableCamera();
    return;
  }
  $("camera").parentElement.classList.add("has-camera");
  feedback = "Preparing hand tracking…";
  render();
  try {
    await initTracker();
  } catch (error) {
    if (generation === cameraGeneration) {
      errorText = error.message;
      disableCamera();
    }
    return;
  }
  if (generation !== cameraGeneration) return;
  calibrated = false;
  alignRollPending = true;
  mapper.heightRange = null;
  calibration.reset();
  phase = "calibrating";
  lastVideoTime = -1;
  lastResultAt = performance.now();
  render();
  requestAnimationFrame(capture);
}

function disableCamera() {
  pause("Camera is off. Your arm holds here.");
  ++cameraGeneration;
  stream?.getTracks().forEach((t) => {
    t.onended = null;
    t.stop();
  });
  stream = null;
  video.srcObject = null;
  worker?.terminate();
  worker = null;
  trackerReady = false;
  busy = false;
  calibrated = false;
  lastSample = null;
  phase = "off";
  progress = 0;
  timestamps = [];
  inferenceTimes = [];
  rate = inferenceMs = 0;
  context.clearRect(0, 0, overlay.width, overlay.height);
  $("camera-placeholder").hidden = false;
  $("camera").parentElement.classList.remove("has-camera");
  render();
}

function recenter() {
  if (!stream || !trackerReady) return;
  pause();
  calibrated = false;
  alignRollPending = true;
  mapper.heightRange = null;
  calibration.reset();
  phase = "calibrating";
  feedback = personal
    ? "Hold your relaxed hand where you want to start."
    : "Lower your palm to the ground line and hold steady.";
  render();
}

function render() {
  $("connection").classList.toggle("online", connected && sceneReady);
  $("connection").innerHTML =
    `<i></i>${connected && sceneReady ? "MARS connected" : "Connecting"}`;
  const handFresh =
    !!lastSample?.valid && performance.now() - lastResultAt < 500;
  $("hand-status").textContent = !stream
    ? "Camera off"
    : handFresh
      ? "Hand tracked"
      : "Tracking on hold";
  $("hand-status").classList.toggle("good", !!stream && handFresh);
  $("feedback").textContent = feedback;
  $("error").hidden = !errorText;
  $("error").textContent = errorText;
  const labels = {
    off: [
      "Ready when you are",
      "Enable your camera to get started.",
      "Enable camera",
    ],
    loading: [
      "A moment to connect",
      "Your camera and tracker are getting ready.",
      "Preparing camera…",
    ],
    calibrating: [
      personal ? "Choose your starting pose" : "Set your ground level",
      personal?.rotation
        ? "Separate your thumb and index slightly. Hold still to align the jaws."
        : personal
          ? "Hold your relaxed hand comfortably in view."
          : "Lower your palm to the line. Keep it in view.",
      "Calibrating…",
    ],
    ready: [
      "Your arm is on hold",
      "Start following to connect your hand.",
      "Start following",
    ],
    following: [
      "Following your hand",
      "Move, turn and tilt. Pinch to grip.",
      "Pause following",
    ],
    holding: [
      "Holding your position",
      "Bring your hand back. Following resumes automatically.",
      "Pause following",
    ],
  }[phase];
  $("stage-title").textContent = labels[0];
  $("stage-detail").textContent = labels[1];
  const primary = $("primary");
  primary.innerHTML = `${!stream ? CAMERA_ICON : armed ? PAUSE_ICON : PLAY_ICON}<span>${labels[2]}</span>`;
  primary.disabled =
    !connected || !sceneReady || ["loading", "calibrating"].includes(phase);
  primary.classList.toggle("following", armed);
  $("state-orb").classList.toggle("live", phase === "following");
  $("state-orb").innerHTML = phase === "following" ? PLAY_ICON : PAUSE_ICON;
  $("recenter").disabled = !stream || !trackerReady;
  $("camera-off").disabled = !stream && phase !== "loading";
  $("calibration-overlay").hidden =
    !stream || !["calibrating", "holding"].includes(phase);
  $("calibration-label").textContent =
    phase === "holding"
      ? "Hand tracking on hold"
      : personal
        ? "Hold your relaxed hand here"
        : "Lower your palm to the line";
  const bounds = mapper.heightRange || calibration.bounds;
  $("ground-guide").hidden = !stream || !bounds;
  if (bounds) $("ground-guide").style.top = `${bounds.bottom * 100}%`;
  $("height-value").textContent =
    state.height_mm < 4 ? "Ground" : `${Math.round(state.height_mm || 0)} mm`;
  $("progress-ring").style.strokeDashoffset = String(164 * (1 - progress));
  for (const [i, id] of [
    "step-camera",
    "step-calibrate",
    "step-move",
  ].entries()) {
    const current = !stream ? 0 : !calibrated ? 1 : 2;
    $(id).className = i === current ? "current" : i < current ? "done" : "";
  }
  $("rate").textContent = rate ? String(rate) : "—";
  const grip = Math.round((state.grip ?? 1) * 100);
  $("grip-value").textContent =
    grip < 5 ? "Closed" : grip > 95 ? "Open" : `${grip}% open`;
  $("grip-meter").style.setProperty("--opening", `${grip}%`);
  $("grip-meter").setAttribute("aria-valuenow", String(grip));
  $("latency").textContent = inferenceMs
    ? String(Math.round(inferenceMs))
    : "—";
}

$("primary").onclick = () =>
  !stream ? enableCamera() : armed ? pause() : follow();
$("camera-off").onclick = disableCamera;
$("recenter").onclick = recenter;
$("view-reset").onclick = () => scene?.resetView();
$("sensitivity").oninput = (event) => {
  mapper.sensitivity = Number(event.target.value);
  if (personal) personal.sensitivity = mapper.sensitivity;
  $("sensitivity-value").textContent = `${mapper.sensitivity.toFixed(1)}×`;
};
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    pause("Stopped. Start following when you’re ready.");
    return;
  }
  if (event.repeat || /INPUT|TEXTAREA|SELECT/.test(event.target.tagName))
    return;
  if (event.code === "Space") {
    if (event.target.tagName === "BUTTON") return;
    event.preventDefault();
    if (armed) pause();
    else follow();
  }
  if (event.key.toLowerCase() === "r") recenter();
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden) pause("Paused while this tab was away.");
});
window.addEventListener("pagehide", disableCamera);
setInterval(() => {
  const now = performance.now();
  if (armed && token) {
    if (now - lastResultAt > 300 && phase === "following")
      holdTracking("Camera catching up · your arm holds here");
    if (now - lastHeartbeat > 500) {
      send({ op: "keepalive", token });
      lastHeartbeat = now;
    }
  }
  if (connected && now - lastStateAt > 2000) {
    connected = false;
    pause("Reconnecting to the simulation…");
    ws?.close();
  }
}, 100);

connect();
createScene($("scene"))
  .then((value) => {
    scene = value;
    sceneReady = true;
    scene.update(
      state.joints
        ? state
        : {
            joints: {},
            pose: [0, 0, 0],
            target: null,
            active: false,
            ee: [0, 0, 0],
          },
    );
    $("scene-loading").hidden = true;
    render();
  })
  .catch((error) => {
    errorText = `Unable to load the 3D workspace. ${error.message}`;
    $("scene-loading").textContent = "Workspace unavailable";
    render();
  });
render();
