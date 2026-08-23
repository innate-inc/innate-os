// @ts-check

import { INPUT_TELEMETRY_TOPIC } from "../constants.js";

const STALE_AFTER_MS = 2_500;

/**
 * @param {HTMLElement} stage
 * @param {import("../rosClient.js").RosClient} ros
 * @returns {{ setVisible: (visible: boolean) => void, destroy: () => void }}
 */
export function createAudioOverlay(stage, ros) {
  const root = document.createElement("aside");
  root.className = "audio-debug";
  root.setAttribute("aria-label", "Live audio diagnostics");
  root.innerHTML = `
    <div class="audio-debug-head">
      <span class="audio-debug-dot"></span>
      <span class="audio-debug-title mono">AUDIO DEBUG</span>
      <span class="audio-debug-state mono">WAITING</span>
    </div>
    <div class="audio-debug-meter">
      <span class="audio-debug-level"></span>
      <span class="audio-debug-threshold"></span>
    </div>
    <div class="audio-debug-values mono">
      <span class="audio-debug-engine">NO TELEMETRY</span>
      <span class="audio-debug-reading">LEVEL —</span>
    </div>
    <div class="audio-debug-event">Waiting for microphone telemetry…</div>
    <div class="audio-debug-stats mono"></div>
  `;
  stage.append(root);

  const state = /** @type {HTMLElement} */ (root.querySelector(".audio-debug-state"));
  const meter = /** @type {HTMLElement} */ (root.querySelector(".audio-debug-meter"));
  const level = /** @type {HTMLElement} */ (root.querySelector(".audio-debug-level"));
  const threshold = /** @type {HTMLElement} */ (root.querySelector(".audio-debug-threshold"));
  const engine = /** @type {HTMLElement} */ (root.querySelector(".audio-debug-engine"));
  const reading = /** @type {HTMLElement} */ (root.querySelector(".audio-debug-reading"));
  const event = /** @type {HTMLElement} */ (root.querySelector(".audio-debug-event"));
  const stats = /** @type {HTMLElement} */ (root.querySelector(".audio-debug-stats"));

  /** @type {number | null} */
  let staleTimer = null;
  let stale = true;
  let lastLevel = 0;
  let lastThreshold = 0;
  let lastBackend = "";
  let lastEngine = "";
  let voiced = false;
  let utteranceOpen = false;
  let utteranceSeconds = 0;
  let ducking = false;
  let vendorVad = false;
  let transcribing = false;
  let lastTranscribeMs = 0;
  let audioQueueChunks = 0;
  let droppedAudioChunks = 0;

  function refreshState() {
    let name = "LISTENING";
    if (stale) name = "STALE";
    else if (ducking) name = "MUTED";
    else if (transcribing) name = "TRANSCRIBING";
    else if (voiced || utteranceOpen) name = "HEARING";
    root.dataset.state = name.toLowerCase();
    state.textContent = name;
  }

  function refreshMeter() {
    meter.hidden = vendorVad;
    level.style.width = `${Math.min(1, Math.max(0, lastLevel)) * 100}%`;
    threshold.style.left = `${Math.min(1, Math.max(0, lastThreshold)) * 100}%`;
    engine.textContent = [lastBackend, lastEngine].filter(Boolean).join(" · ").toUpperCase() || "NO TELEMETRY";
    reading.textContent = vendorVad
      ? "VENDOR VAD"
      : `LEVEL ${lastLevel.toFixed(3)} · TRIGGER ${lastThreshold.toFixed(3)}`;
  }

  function refreshStats() {
    const values = [];
    if (utteranceOpen) values.push(`CLIP ${utteranceSeconds.toFixed(1)}s`);
    if (lastTranscribeMs) values.push(`LAST STT ${formatDuration(lastTranscribeMs)}`);
    if (audioQueueChunks) values.push(`BUFFER ${audioQueueChunks}`);
    if (droppedAudioChunks) values.push(`DROPPED ${droppedAudioChunks}`);
    stats.textContent = values.join("  ·  ");
    stats.hidden = values.length === 0;
  }

  /** @param {string} text */
  function showEvent(text) {
    event.textContent = text;
    event.classList.remove("flash");
    void event.offsetWidth;
    event.classList.add("flash");
  }

  function markFresh() {
    stale = false;
    if (staleTimer !== null) clearTimeout(staleTimer);
    staleTimer = window.setTimeout(() => {
      stale = true;
      refreshState();
      showEvent("Microphone telemetry stopped");
    }, STALE_AFTER_MS);
  }

  /** @param {any} data */
  function onVad(data) {
    markFresh();
    lastBackend = String(data.backend ?? "");
    lastEngine = String(data.engine ?? "");
    vendorVad = lastEngine === "vendor";
    lastLevel = Number(data.level) || 0;
    lastThreshold = Number(data.threshold) || 0;
    voiced = Boolean(data.voiced);
    utteranceOpen = Boolean(data.utterance_open);
    utteranceSeconds = Number(data.utterance_secs) || 0;
    ducking = Boolean(data.ducking);
    refreshState();
    refreshMeter();
    refreshStats();
  }

  /** @param {any} data */
  function onDebug(data) {
    markFresh();
    lastBackend = String(data.backend ?? lastBackend);
    lastEngine = String(data.engine ?? lastEngine);
    if (data.audio_queue_chunks != null) audioQueueChunks = Number(data.audio_queue_chunks) || 0;
    if (data.dropped_audio_chunks != null) droppedAudioChunks = Number(data.dropped_audio_chunks) || 0;
    const phase = String(data.phase ?? "");
    if (phase === "speech_started") {
      transcribing = false;
      showEvent(data.source === "tts" ? "MARS is generating speech" : "Speech detected");
    } else if (phase === "utterance_closed") {
      transcribing = true;
      showEvent(`Utterance closed · ${formatSeconds(data.audio_seconds)} audio`);
    } else if (phase === "transcript_ready") {
      transcribing = false;
      lastTranscribeMs = Number(data.total_ms) || 0;
      showEvent(`Transcript ready in ${formatDuration(data.total_ms)}`);
    } else if (phase === "no_speech" || phase === "utterance_rejected") {
      transcribing = false;
      showEvent(phase === "no_speech" ? "No speech recognized" : "Not enough voiced audio");
    } else if (phase === "utterance_dropped") {
      transcribing = false;
      showEvent("STT backlog dropped an utterance");
    } else if (phase === "transcription_failed") {
      transcribing = false;
      showEvent("Transcription failed");
    } else if (phase === "ducking_started") {
      ducking = true;
      showEvent("Mic muted while MARS speaks");
    } else if (phase === "ducking_ended") {
      ducking = false;
      showEvent(`Mic resumed after ${formatDuration(data.duration_ms)}`);
    } else if (phase === "speech_completed" && data.source === "tts") {
      showEvent(`MARS spoke for ${formatSeconds(data.playback_seconds)}`);
    } else if (phase === "speech_failed" && data.source === "tts") {
      showEvent("MARS speech failed");
    }
    refreshState();
    refreshMeter();
    refreshStats();
  }

  const unsubscribe = ros.subscribe(
    INPUT_TELEMETRY_TOPIC,
    (/** @type {any} */ msg) => {
      if (typeof msg?.data !== "string") return;
      try {
        const data = JSON.parse(msg.data);
        if (data?.kind === "vad_status" && data?.input_device === "micro") onVad(data);
        else if (data?.kind === "speech_debug") onDebug(data);
      } catch {
        return;
      }
    },
    0,
    "std_msgs/msg/String",
  );

  refreshState();
  refreshMeter();
  refreshStats();

  return {
    setVisible(visible) {
      root.hidden = !visible;
    },
    destroy() {
      unsubscribe();
      if (staleTimer !== null) clearTimeout(staleTimer);
      root.remove();
    },
  };
}

/** @param {unknown} value */
function formatDuration(value) {
  const ms = Number(value);
  if (!Number.isFinite(ms)) return "unknown";
  return ms < 1_000 ? `${Math.round(ms)}ms` : `${(ms / 1_000).toFixed(ms < 10_000 ? 2 : 1)}s`;
}

/** @param {unknown} value */
function formatSeconds(value) {
  const seconds = Number(value);
  return Number.isFinite(seconds) ? `${seconds.toFixed(seconds < 10 ? 2 : 1)}s` : "unknown";
}
