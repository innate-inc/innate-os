import "./match.css";
import { createScene } from "./scene.js";
import { measureHand } from "./control.mjs";

const $ = (id) => document.getElementById(id);
const video = $("camera"),
  overlay = $("landmarks"),
  ctx = overlay.getContext("2d");
const FLOOR = new URLSearchParams(location.search).get("focus") === "floor";
const STUDY = FLOOR ? "floor" : "general";
const KEY = FLOOR
  ? "innate-floor-matching-session-v1"
  : "innate-pose-matching-session-v1";
if (FLOOR) {
  document.title = "Teach floor grasps · Innate";
  document.querySelector(".product-name").textContent =
    "Fine-tune floor grasps";
  document.querySelector(".eyebrow").textContent = "A DEEPER TILT. YOUR WAY.";
  $("step-count").textContent = "7 poses · about 2 minutes";
  $("instruction").textContent =
    "Imagine your thumb and index are the claw. Show the gesture that feels natural for each tilt, grasp and lift.";
  document.querySelector("#complete > p").innerHTML =
    "Tell me <strong>“floor poses done”</strong>. I’ll use these matches to tune the deeper pitch and check that pinching and lifting still feel natural.";
}
let scene,
  ws,
  session,
  poses = [],
  matches = {},
  index = 0,
  state,
  token,
  sequence = 0;
let stream,
  worker,
  trackerReady = false,
  busy = false,
  lastVideoTime = -1,
  frameMeta,
  sample,
  sampleAt = 0;
let phase = "off",
  draft,
  recorder,
  frames = [],
  totalFrames = 0,
  clipStart = 0,
  clipVideoStart = 0;
let requestId = 0,
  countdown = 0,
  actionGeneration = 0,
  selectedAt = 0,
  sideView = false,
  connected = false,
  firstOpen = true;
let recovering = false;
const pending = new Map();
const pose = () => poses[index];
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const error = (text) => {
  $("error").textContent = text;
  $("error").hidden = !text;
};
const hint = (text) => {
  $("feedback").textContent = text;
};

function rpc(message) {
  if (ws?.readyState !== WebSocket.OPEN)
    return Promise.reject(
      new Error(
        "Reconnecting. Your take is kept here; try again when connected.",
      ),
    );
  const request = ++requestId;
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      pending.delete(request);
      reject(
        new Error(
          "No save confirmation yet. Your take is still here; please retry.",
        ),
      );
    }, 15000);
    pending.set(request, { resolve, reject, timeout });
    ws.send(JSON.stringify({ ...message, study: STUDY, request }));
  });
}
function settled() {
  return (
    !!state &&
    !!pose() &&
    !!token &&
    performance.now() - selectedAt > 600 &&
    Object.entries(pose().joints).every(
      ([name, target]) => Math.abs(state.joints[name] - target) < 0.04,
    )
  );
}
function readyHand() {
  return (
    sample?.valid && sample.gripValid && performance.now() - sampleAt < 450
  );
}
function stopDemo() {
  if (token && ws?.readyState === WebSocket.OPEN)
    ws.send(JSON.stringify({ op: "stop" }));
  token = null;
}
async function lease() {
  const generation = actionGeneration;
  const response = await rpc({ op: "begin", mode: "study" });
  if (
    generation !== actionGeneration ||
    ["paused", "complete"].includes(phase)
  ) {
    if (ws.readyState === WebSocket.OPEN)
      ws.send(JSON.stringify({ op: "stop" }));
    throw new Error("Pose matching paused.");
  }
  token = response.token;
  sequence = 0;
  selectedAt = performance.now();
  ws.send(
    JSON.stringify({
      op: "study_pose",
      token,
      pose_id: pose().id,
      sequence: ++sequence,
      captured_at: Date.now(),
    }),
  );
}
async function recoverLease() {
  if (recovering || !stream || ["paused", "complete", "off"].includes(phase))
    return;
  recovering = true;
  token = null;
  if (["countdown", "recording"].includes(phase)) cancelCapture();
  hint("Restoring this pose. Your reviewed take stays here.");
  try {
    await lease();
    error("");
    hint(
      draft
        ? "Your take is ready to keep."
        : "Pose restored. Record when your gesture feels right.",
    );
  } catch (e) {
    phase = "paused";
    error(e.message);
  } finally {
    recovering = false;
    render();
  }
}
function connect() {
  ws = new WebSocket(
    `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/control`,
  );
  ws.onmessage = async ({ data }) => {
    const message = JSON.parse(data);
    if (message.type === "state") {
      state = message;
      scene?.update({ ...state, active: false });
      render();
      return;
    }
    if (message.type === "hello") {
      connected = true;
      render();
      try {
        let previous;
        try {
          previous = localStorage.getItem(KEY);
        } catch {}
        const opened = await rpc({
          op: "study_open",
          session: session || previous || undefined,
        });
        session = opened.session;
        poses = opened.poses;
        matches = opened.matches;
        try {
          localStorage.setItem(KEY, session);
        } catch {}
        if (firstOpen) {
          index = Math.max(
            0,
            poses.findIndex((p) => !matches[p.id]),
          );
          firstOpen = false;
          if (Object.keys(matches).length === poses.length) phase = "complete";
        }
        if (stream && !["complete", "paused", "loading", "off"].includes(phase))
          await lease();
        render();
      } catch (e) {
        error(e.message);
      }
    }
    const request = pending.get(message.request);
    if (request) {
      clearTimeout(request.timeout);
      pending.delete(message.request);
      message.type === "error"
        ? request.reject(new Error(message.message))
        : request.resolve(message);
    } else if (message.type === "error") {
      if (
        message.message === "Start following before sending movement" &&
        stream &&
        !["paused", "complete"].includes(phase)
      ) {
        await recoverLease();
        return;
      }
      error(message.message);
      token = null;
      phase = "paused";
      render();
    }
  };
  ws.onclose = () => {
    connected = false;
    token = null;
    for (const item of pending.values()) {
      clearTimeout(item.timeout);
      item.reject(
        new Error(
          "Connection interrupted. Your take is still here; reconnecting…",
        ),
      );
    }
    pending.clear();
    if (["countdown", "recording"].includes(phase)) cancelCapture();
    render();
    setTimeout(connect, 800);
  };
}
setInterval(() => {
  if (
    token &&
    pose() &&
    connected &&
    !document.hidden &&
    phase !== "complete" &&
    phase !== "paused" &&
    ws.bufferedAmount < 65536
  ) {
    ws.send(
      JSON.stringify({
        op: "study_pose",
        token,
        pose_id: pose().id,
        sequence: ++sequence,
        captured_at: Date.now(),
      }),
    );
  }
  render();
}, 100);

function render() {
  $("connection").textContent = connected ? "MARS connected" : "Reconnecting…";
  $("saved-count").textContent =
    `${Object.keys(matches).length} / ${poses.length || (FLOOR ? 7 : 16)} saved`;
  $("progress").value = Object.keys(matches).length;
  $("progress").max = poses.length || (FLOOR ? 7 : 16);
  $("hand-status").textContent = !stream
    ? "Camera off"
    : readyHand()
      ? "Hand in view"
      : "Keep one hand in view";
  $("hand-status").classList.toggle("good", !!readyHand());
  $("robot-status").textContent = !scene
    ? "Preparing MARS…"
    : !token
      ? "Robot is holding"
      : settled()
        ? "Pose ready · choose your natural gesture"
        : "Moving into this pose…";
  if (pose()) {
    $("pose-title").textContent =
      phase === "complete" ? "Your intuition, captured." : pose().title;
    $("step-count").textContent = `Pose ${index + 1} of ${poses.length}`;
  }
  $("pose-prompt").textContent =
    phase === "complete"
      ? "Next, we learn the mapping from your matches."
      : "Show the hand pose that would make you expect the robot to look like this.";
  $("progress-label").textContent = matches[pose()?.id]
    ? `Previously saved · ${matches[pose().id].comfort}`
    : "Your instinct is the reference";
  $("review").hidden = !["review", "saving"].includes(phase);
  $("complete").hidden = phase !== "complete";
  $("instruction").hidden = phase === "complete";
  $("primary").hidden = ["review", "saving", "complete"].includes(phase);
  $("shortcut").hidden = ["review", "saving", "complete"].includes(phase);
  $("primary").textContent =
    phase === "off"
      ? "Enable camera & start"
      : phase === "loading"
        ? "Preparing camera…"
        : phase === "paused"
          ? "Resume pose matching"
          : phase === "countdown"
            ? "Get into your pose…"
            : phase === "recording"
              ? "Hold your natural pose…"
              : !settled()
                ? "Robot is getting ready…"
                : !readyHand()
                  ? "Show one hand to record"
                  : "Record my gesture";
  $("primary").disabled =
    !scene ||
    !connected ||
    !poses.length ||
    ["loading", "countdown", "recording"].includes(phase) ||
    (phase === "ready" && (!settled() || !readyHand()));
  const locked = ["loading", "countdown", "recording", "saving"].includes(
    phase,
  );
  $("new-session").disabled = locked || !connected;
  $("side-view").disabled = locked;
  $("scene").style.pointerEvents = locked ? "none" : "";
  $("back").disabled = locked || index === 0 || !poses.length;
  $("skip").disabled = locked || !token || !settled() || phase === "complete";
  $("comfortable").disabled = $("awkward").disabled =
    phase === "saving" || !connected || !token;
  $("retry").disabled = phase === "saving";
  $("countdown").hidden = phase !== "countdown";
  $("countdown").textContent = countdown;
  $("record-light").hidden = phase !== "recording";
  if (phase === "complete")
    $("session-summary").textContent =
      `${Object.values(matches).filter((m) => m.comfort !== "unmatchable").length} recorded matches · ${Object.values(matches).filter((m) => m.comfort === "unmatchable").length} marked difficult to match`;
}

function draw(points) {
  overlay.width = video.videoWidth;
  overlay.height = video.videoHeight;
  ctx.clearRect(0, 0, overlay.width, overlay.height);
  if (!points) return;
  ctx.strokeStyle = "#c8ffe1";
  ctx.fillStyle = "white";
  ctx.lineWidth = 2;
  for (const finger of [
    [0, 1, 2, 3, 4],
    [0, 5, 6, 7, 8],
    [0, 9, 10, 11, 12],
    [0, 13, 14, 15, 16],
    [0, 17, 18, 19, 20],
  ]) {
    ctx.beginPath();
    finger.forEach((i, n) =>
      n
        ? ctx.lineTo(points[i].x * overlay.width, points[i].y * overlay.height)
        : ctx.moveTo(points[i].x * overlay.width, points[i].y * overlay.height),
    );
    ctx.stroke();
  }
  for (const p of points) {
    ctx.beginPath();
    ctx.arc(p.x * overlay.width, p.y * overlay.height, 3, 0, Math.PI * 2);
    ctx.fill();
  }
}
function cameraFrame(now) {
  if (!stream) return;
  requestAnimationFrame(cameraFrame);
  if (
    !trackerReady ||
    busy ||
    document.hidden ||
    video.readyState < 2 ||
    video.currentTime === lastVideoTime
  )
    return;
  busy = true;
  lastVideoTime = video.currentTime;
  frameMeta = { captured_at: Date.now(), video_time: video.currentTime };
  createImageBitmap(video)
    .then((bitmap) => {
      if (!worker) {
        bitmap.close();
        busy = false;
        return;
      }
      worker.postMessage(
        {
          type: "frame",
          bitmap,
          timestamp: now,
          capturedAt: frameMeta.captured_at,
          generation: 1,
        },
        [bitmap],
      );
    })
    .catch(() => {
      busy = false;
    });
}
async function enableCamera() {
  const generation = ++actionGeneration;
  phase = "loading";
  error("");
  render();
  try {
    if (!stream) {
      const acquired = await navigator.mediaDevices.getUserMedia({
        audio: false,
        video: {
          width: { ideal: 640 },
          height: { ideal: 480 },
          frameRate: { ideal: 30, max: 30 },
          facingMode: "user",
        },
      });
      if (generation !== actionGeneration) {
        acquired.getTracks().forEach((t) => t.stop());
        return;
      }
      stream = acquired;
      video.srcObject = stream;
      await video.play();
      $("camera-empty").hidden = true;
      worker = new Worker(new URL("./tracker.worker.js", import.meta.url));
      await new Promise((resolve, reject) => {
        const timeout = setTimeout(
          () =>
            reject(
              new Error("Hand tracker took too long to load. Please retry."),
            ),
          20000,
        );
        worker.onmessage = ({ data }) => {
          if (data.type === "ready") {
            clearTimeout(timeout);
            trackerReady = true;
            resolve();
            return;
          }
          if (data.type === "error") {
            busy = false;
            clearTimeout(timeout);
            reject(new Error(data.message));
            error(data.message);
            return;
          }
          if (data.type !== "result") return;
          busy = false;
          sample = measureHand(
            data.result,
            video.videoWidth,
            video.videoHeight,
          );
          sampleAt = performance.now();
          draw(sample.raw);
          if (phase === "recording" && frameMeta.captured_at >= clipStart) {
            totalFrames++;
            if (sample.valid && sample.gripValid)
              frames.push({
                ...frameMeta,
                video_time: frameMeta.video_time - clipVideoStart,
                inference_ms: data.inferenceMs,
                landmarks: data.result.landmarks[0],
                world_landmarks: data.result.worldLandmarks?.[0] || null,
                handedness: data.result.handedness?.[0] || [],
                robot_joints: state?.joints,
              });
          }
          render();
        };
        worker.postMessage({
          type: "init",
          base: new URL("/", location.href).href,
        });
      });
      stream.getVideoTracks()[0].onended = () => {
        cancelCapture();
        shutdownCamera();
        phase = "off";
        error("Camera disconnected. Reconnect it and enable the camera again.");
        render();
      };
      requestAnimationFrame(cameraFrame);
    }
    if (generation !== actionGeneration) return;
    await lease();
    phase = draft ? "review" : "ready";
    hint(
      "Take your time. Make the pose you would naturally use, then press Space to record.",
    );
    render();
  } catch (e) {
    if (generation !== actionGeneration) return;
    shutdownCamera();
    phase = "off";
    error(
      e.name === "NotAllowedError"
        ? "Camera access was declined. Allow it for this page and try again."
        : e.message,
    );
    render();
  }
}
function clearDraft() {
  if (draft?.url) URL.revokeObjectURL(draft.url);
  draft = null;
  $("take-preview").pause();
  $("take-preview").removeAttribute("src");
  $("take-preview").hidden = true;
  $("notes").value = "";
}
function cancelCapture() {
  actionGeneration++;
  if (recorder?.state === "recording") recorder.stop();
  recorder = null;
  phase = draft ? "review" : stream ? "ready" : "off";
}
async function record() {
  if (!readyHand() || !settled()) return;
  const generation = ++actionGeneration;
  error("");
  phase = "countdown";
  for (const n of [2, 1]) {
    countdown = n;
    render();
    await delay(1000);
    if (generation !== actionGeneration) return;
  }
  if (!readyHand() || !settled()) {
    phase = "ready";
    hint("Keep your hand visible and let the robot settle, then try again.");
    render();
    return;
  }
  try {
    const mime = [
      "video/webm;codecs=vp8",
      "video/webm;codecs=vp9",
      "video/webm",
      "video/mp4",
    ].find((type) => MediaRecorder.isTypeSupported(type));
    if (!mime)
      throw new Error(
        "This browser cannot record a camera take. Open the studio in Chrome.",
      );
    const chunks = [];
    frames = [];
    totalFrames = 0;
    const rec = new MediaRecorder(stream, {
      mimeType: mime,
      videoBitsPerSecond: 700000,
    });
    recorder = rec;
    rec.ondataavailable = (e) => {
      if (e.data.size) chunks.push(e.data);
    };
    const stopped = new Promise((resolve, reject) => {
      rec.onstop = resolve;
      rec.onerror = () =>
        reject(new Error("Camera recording failed. Please retry."));
    });
    clipStart = Date.now();
    clipVideoStart = video.currentTime;
    const robot = structuredClone(state);
    const robotView = scene.viewState();
    phase = "recording";
    rec.start();
    render();
    await delay(2000);
    if (rec.state === "recording") rec.stop();
    await stopped;
    recorder = null;
    if (generation !== actionGeneration) return;
    if (frames.length < 8 || frames.length / Math.max(1, totalFrames) < 0.8) {
      phase = "ready";
      hint(
        "Tracking missed too much of that take. Keep thumb and index visible and record again.",
      );
      render();
      return;
    }
    const blob = new Blob(chunks, { type: mime });
    draft = {
      blob,
      mime,
      frames: [...frames],
      camera: {
        width: video.videoWidth,
        height: video.videoHeight,
        frame_rate: stream.getVideoTracks()[0].getSettings().frameRate,
        mirrored_preview: true,
        robot_view: { start: robotView, end: scene.viewState() },
        tracker: "MediaPipe Hand Landmarker 0.10.32",
        clip_start_ms: clipStart,
        total_inferences: totalFrames,
      },
      robot_at_capture: { start: robot, end: structuredClone(state) },
      url: URL.createObjectURL(blob),
    };
    $("take-preview").src = draft.url;
    $("take-preview").hidden = false;
    await $("take-preview").play();
    phase = "review";
    hint(
      "Review your take. Keep the match if it represents what felt intuitive to you.",
    );
    render();
  } catch (e) {
    phase = "ready";
    error(e.message);
    render();
  }
}
const asBase64 = (blob) =>
  new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result.split(",")[1]);
    reader.onerror = reject;
    reader.readAsDataURL(blob);
  });
async function save(comfort) {
  if (!token || (comfort !== "unmatchable" && !draft)) return;
  phase = "saving";
  error("");
  render();
  try {
    const response = await rpc({
      op: "study_save",
      token,
      session,
      pose_id: pose().id,
      comfort,
      notes: $("notes").value,
      ...(comfort !== "unmatchable"
        ? {
            video: await asBase64(draft.blob),
            mime: draft.mime,
            frames: draft.frames,
            camera: draft.camera,
            robot_at_capture: draft.robot_at_capture,
          }
        : {}),
    });
    matches = response.matches;
    clearDraft();
    hint("Match saved on this Mac.");
    const next = poses.findIndex((p) => !matches[p.id]);
    if (next < 0) {
      phase = "complete";
      shutdownCamera();
      stopDemo();
    } else {
      index = next;
      selectedAt = performance.now();
      phase = "ready";
    }
    render();
  } catch (e) {
    phase = draft ? "review" : "ready";
    error(e.message);
    render();
  }
}
function shutdownCamera() {
  stream?.getTracks().forEach((t) => {
    t.onended = null;
    t.stop();
  });
  stream = null;
  worker?.terminate();
  worker = null;
  trackerReady = false;
  busy = false;
  video.srcObject = null;
  $("camera-empty").hidden = false;
  stopDemo();
}
function previous() {
  if (index > 0) {
    clearDraft();
    index--;
    selectedAt = performance.now();
    phase = stream ? "ready" : "off";
    error("");
    render();
  }
}
$("primary").onclick = () =>
  phase === "off" || phase === "paused" ? enableCamera() : record();
$("comfortable").onclick = () => save("comfortable");
$("awkward").onclick = () => save("awkward");
$("skip").onclick = () => save("unmatchable");
$("retry").onclick = () => {
  clearDraft();
  phase = "ready";
  render();
};
$("back").onclick = previous;
$("side-view").onclick = () => {
  sideView = !sideView;
  scene?.studyView(sideView, FLOOR);
  $("side-view").textContent = sideView
    ? "Return to front angle"
    : "View from the side";
};
$("review-start").onclick = () => {
  index = 0;
  phase = "off";
  render();
};
$("new-session").onclick = async () => {
  cancelCapture();
  clearDraft();
  shutdownCamera();
  try {
    const opened = await rpc({ op: "study_open" });
    session = opened.session;
    matches = opened.matches;
    poses = opened.poses;
    try {
      localStorage.setItem(KEY, session);
    } catch {}
    index = 0;
    phase = "off";
    error("");
    hint("New session ready. Previous recordings are still saved.");
    render();
  } catch (e) {
    error(e.message);
  }
};
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    const loading = phase === "loading";
    cancelCapture();
    if (loading) shutdownCamera();
    stopDemo();
    phase = "paused";
    hint("Paused. Resume whenever you’re ready.");
    render();
    return;
  }
  if (/INPUT|TEXTAREA|BUTTON/.test(e.target.tagName) || e.repeat) return;
  if (e.code === "Space") {
    e.preventDefault();
    if (phase === "ready") record();
  }
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    cancelCapture();
    stopDemo();
    if (stream) phase = "paused";
    render();
  }
});
window.addEventListener("pagehide", () => {
  cancelCapture();
  shutdownCamera();
});
createScene($("scene"))
  .then((value) => {
    scene = value;
    sideView = FLOOR;
    scene.studyView(sideView, FLOOR);
    $("side-view").textContent = sideView
      ? "Return to front angle"
      : "View from the side";
    if (state) scene.update({ ...state, active: false });
    render();
  })
  .catch((e) => error(e.message));
connect();
render();
