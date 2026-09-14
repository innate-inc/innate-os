const $ = (id) => document.getElementById(id);
let config, stream, recorder, pending, playbackURL, countdownTimer, recordingTimer;
let phase = "idle", index = 0, started = 0, session = crypto.randomUUID();
let saved = new Set();
const status = (message) => { $("status").textContent = message; };

function render() {
  const clip = config.protocol.clips[index];
  $("title").textContent = clip?.title ?? "Session complete";
  $("instruction").textContent = clip?.instruction ?? "Choose the next session above and record a fresh set. After all three sessions, return to Codex and say ‘recordings ready’.";
  $("cue").textContent = clip ? `${clip.title} · ${clip.instruction}` : "Session complete. All clips saved.";
  $("step-number").textContent = clip ? `CLIP ${index + 1} OF ${config.protocol.clips.length} · 8 SECONDS` : "ALL CLIPS SAVED";
  $("record").disabled = !stream || !clip || phase !== "idle";
  $("record").hidden = phase === "review";
  $("stop").hidden = !["countdown", "recording"].includes(phase);
  $("review-actions").hidden = phase !== "review";
  for (const name of ["split", "hand", "devices", "camera"]) $(name).disabled = phase !== "idle";
  $("off").disabled = !stream || phase === "review" || phase === "saving";
  $("clips").replaceChildren(...config.protocol.clips.map((c, i) => {
    const el = document.createElement("li"); el.textContent = c.title;
    el.className = saved.has(i) ? "done" : i === index ? "active" : "";
    return el;
  }));
}

function stopCamera() {
  if (phase === "countdown") cancelCountdown();
  if (phase === "recording") finish(false);
  stream?.getTracks().forEach((track) => track.stop());
  stream = null; $("preview").srcObject = null;
  $("placeholder").hidden = phase === "review";
  $("camera-info").textContent = "Camera off";
  render();
}

async function enableCamera() {
  stopCamera();
  try {
    const deviceId = $("devices").value;
    stream = await navigator.mediaDevices.getUserMedia({audio: false, video: {
      ...(deviceId ? {deviceId: {exact: deviceId}} : {}),
      width: {ideal: 640}, height: {ideal: 480}, frameRate: {ideal: 30, max: 30}
    }});
    $("preview").srcObject = stream; await $("preview").play();
    $("placeholder").hidden = true;
    const track = stream.getVideoTracks()[0], settings = track.getSettings();
    const devices = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "videoinput");
    $("devices").replaceChildren(...devices.map((d) => new Option(d.label || "Webcam", d.deviceId)));
    $("devices").value = settings.deviceId;
    $("devices").hidden = devices.length < 2;
    $("camera-info").textContent = `${settings.width} × ${settings.height} · requested 30 fps`;
    track.onended = () => { stopCamera(); status("Webcam disconnected. Reconnect it and redo the take."); };
    status("Read the instruction, then record. Keep the webcam fixed.");
  } catch (e) { status(`Could not open webcam: ${e.message}. Open this page in Chrome and allow camera access.`); }
  render();
}

function cancelCountdown() {
  clearInterval(countdownTimer); $("countdown").hidden = true; phase = "idle"; render();
}

function finish(complete) {
  if (phase !== "recording") return;
  clearInterval(recordingTimer);
  pending.metadata.duration_ms = performance.now() - started;
  pending.metadata.complete = complete;
  phase = "finishing";
  recorder.stop();
}

function beginRecording() {
  const mime = ["video/webm;codecs=vp8", "video/webm", "video/mp4"].find((m) => MediaRecorder.isTypeSupported(m));
  if (!mime) { phase = "idle"; render(); return status("This browser cannot record a supported video. Try Chrome."); }
  const chunks = [];
  try {
    recorder = new MediaRecorder(stream, {mimeType: mime, videoBitsPerSecond: 2500000});
  } catch (e) { phase = "idle"; render(); return status(`Could not start recording: ${e.message}`); }
  pending = {metadata: {
    session, recording_id: crypto.randomUUID(), protocol_version: config.protocol.version,
    clip: config.protocol.clips[index].id, split: $("split").value, hand: $("hand").value,
    mime: recorder.mimeType, mirrored_recording: false, mirrored_preview: true,
    camera: (({width, height, frameRate, facingMode}) => ({width, height, frameRate, facingMode}))(stream.getVideoTracks()[0].getSettings()),
    browser: navigator.userAgent, recorded_at: new Date().toISOString(), audio: false
  }};
  recorder.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
  recorder.onerror = (e) => { status(`Recording failed: ${e.error?.message ?? "camera error"}. Redo this take.`); finish(false); };
  recorder.onstop = () => {
    pending.blob = new Blob(chunks, {type: recorder.mimeType});
    if (playbackURL) URL.revokeObjectURL(playbackURL);
    playbackURL = URL.createObjectURL(pending.blob);
    $("review").src = playbackURL; $("review").hidden = false; $("preview").hidden = true;
    $("countdown").hidden = true; $("view-label").textContent = "REVIEW YOUR TAKE · MIRRORED";
    phase = "review";
    $("save").disabled = !pending.metadata.complete || !pending.blob.size;
    status(pending.metadata.complete ? "Review your take. Keep it if you followed the instruction, or redo it." : "Take interrupted. Redo this clip to record the full eight seconds.");
    render();
  };
  phase = "recording";
  started = performance.now(); recorder.start(250);
  render();
  recordingTimer = setInterval(() => {
    const elapsed = performance.now() - started;
    $("countdown").textContent = `${Math.max(0, Math.ceil(config.protocol.seconds - elapsed / 1000))}s`;
    $("progress").style.width = `${Math.min(100, elapsed / (config.protocol.seconds * 10))}%`;
    if (elapsed >= config.protocol.seconds * 1000) finish(true);
  }, 100);
  status("Recording video only. Follow the movement instruction.");
}

function discard() {
  pending = null; phase = "idle";
  $("review").pause(); $("review").removeAttribute("src"); $("review").load();
  if (playbackURL) URL.revokeObjectURL(playbackURL);
  playbackURL = null;
  $("review").hidden = true; $("preview").hidden = false;
  $("placeholder").hidden = Boolean(stream);
  $("view-label").textContent = "MIRRORED PREVIEW";
  $("progress").style.width = "0%";
  render();
}

$("camera").onclick = enableCamera;
$("devices").onchange = enableCamera;
$("off").onclick = () => { stopCamera(); status("Camera off."); };
$("record").onclick = () => {
  phase = "countdown"; let remaining = 3;
  $("countdown").textContent = remaining; $("countdown").hidden = false;
  render();
  countdownTimer = setInterval(() => {
    remaining--; $("countdown").textContent = remaining;
    if (!remaining) { clearInterval(countdownTimer); beginRecording(); }
  }, 1000);
};
$("stop").onclick = () => phase === "countdown" ? cancelCountdown() : finish(false);
$("redo").onclick = () => { discard(); status("Ready for another take."); };
$("save").onclick = async () => {
  phase = "saving"; $("save").disabled = true; $("redo").disabled = true; render(); status("Saving to this computer…");
  try {
    const dataURL = await new Promise((resolve, reject) => {
      const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = reject;
      reader.readAsDataURL(pending.blob);
    });
    const response = await fetch("/api/recordings", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({metadata: pending.metadata, video_base64: dataURL.split(",")[1]})});
    const result = await response.json(); if (!response.ok) throw new Error(result.error);
    saved.add(index); index++; discard();
    status(`Saved ${saved.size} of ${config.protocol.clips.length} clips in this session.`);
  } catch (e) { phase = "review"; status(`Save failed: ${e.message}. Your take is still here; retry Keep & next.`); }
  $("save").disabled = false; $("redo").disabled = false; render();
};
$("split").onchange = () => {
  session = crypto.randomUUID(); saved = new Set(); index = 0;
  status("Fresh session started. Previously saved recordings remain on disk."); render();
};
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    if (phase === "recording") finish(false);
    if (phase === "countdown") cancelCountdown();
  }
});
window.addEventListener("beforeunload", (e) => {
  if (phase !== "idle") { e.preventDefault(); e.returnValue = ""; }
});

try {
  const response = await fetch("/api/config");
  if (!response.ok) throw new Error("recorder server unavailable");
  config = await response.json();
  $("output").textContent = `Saved locally: ${config.output} · No account, cloud upload, or robot connection.`;
  if (!navigator.mediaDevices || !window.MediaRecorder) status("Open this local page in Chrome to enable webcam recording.");
  render();
} catch (e) { status(`Could not load recorder: ${e.message}`); }
