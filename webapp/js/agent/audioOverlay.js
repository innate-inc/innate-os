// @ts-check

import { CHAT_OUT_TOPIC, INPUT_TELEMETRY_TOPIC } from "../constants.js";

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
    <div class="audio-debug-flow mono" aria-label="Speech pipeline">
      <span data-step="heard">HEARD</span>
      <span data-step="stopped">STOP</span>
      <span data-step="text">TEXT</span>
      <span data-step="reply">REPLY</span>
      <span data-step="voice">VOICE</span>
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
  const flowSteps = new Map(
    [...root.querySelectorAll(".audio-debug-flow [data-step]")].map((step) => [
      String(step.getAttribute("data-step")),
      /** @type {HTMLElement} */ (step),
    ]),
  );

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
  let fault = "";
  let vendorVad = false;
  let transcribing = false;
  let lastTranscribeMs = 0;
  let transcriptToReplyMs = 0;
  let transcriptToVoiceMs = 0;
  let stopToVoiceMs = 0;
  let lastStoppedAt = 0;
  let lastTranscriptAt = 0;
  let pendingTranscripts = 0;
  let responseTranscriptCount = 0;
  let replyReportedForTranscriptAt = 0;
  let voiceStartedForTranscriptAt = 0;
  let audioQueueChunks = 0;
  let droppedAudioChunks = 0;

  function refreshState() {
    let name = "LISTENING";
    if (stale) name = "STALE";
    else if (fault) name = fault;
    else if (ducking) name = "MUTED";
    else if (transcribing) name = "TRANSCRIBING";
    else if (voiced || utteranceOpen) name = "HEARING";
    root.dataset.state = fault && !stale ? "error" : name.toLowerCase();
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
    if (lastTranscribeMs) values.push(`STOP→TEXT ${formatDuration(lastTranscribeMs)}`);
    if (transcriptToReplyMs) values.push(`TEXT→REPLY ${formatDuration(transcriptToReplyMs)}`);
    if (transcriptToVoiceMs) values.push(`TEXT→VOICE ${formatDuration(transcriptToVoiceMs)}`);
    if (stopToVoiceMs) values.push(`STOP→VOICE ${formatDuration(stopToVoiceMs)}`);
    if (responseTranscriptCount > 1) values.push(`BUNDLED ${responseTranscriptCount}`);
    if (audioQueueChunks) values.push(`BUFFER ${audioQueueChunks}`);
    if (droppedAudioChunks) values.push(`DROPPED ${droppedAudioChunks}`);
    stats.replaceChildren(
      ...values.map((value) => {
        const chip = document.createElement("span");
        chip.textContent = value;
        return chip;
      }),
    );
    stats.hidden = values.length === 0;
  }

  function resetFlow() {
    for (const step of flowSteps.values()) step.className = "";
  }

  /** @param {string} name @param {"active" | "done" | "error"} status */
  function setFlowStep(name, status) {
    const step = flowSteps.get(name);
    if (step) step.className = status;
  }

  /** @param {string} name */
  function clearFlowError(name) {
    const step = flowSteps.get(name);
    if (step?.classList.contains("error")) step.className = "";
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
    const eventAt = eventTime(data);
    if (phase === "speech_started" && data.source === "stt") {
      if (pendingTranscripts === 0) {
        lastTranscribeMs = 0;
        transcriptToReplyMs = 0;
        transcriptToVoiceMs = 0;
        stopToVoiceMs = 0;
        responseTranscriptCount = 0;
      }
      transcribing = false;
      voiced = true;
      lastStoppedAt = 0;
      resetFlow();
      setFlowStep("heard", "active");
      showEvent("You are talking");
    } else if (phase === "utterance_closed") {
      transcribing = true;
      voiced = false;
      utteranceOpen = false;
      lastStoppedAt = eventAt;
      setFlowStep("heard", "done");
      setFlowStep("stopped", "active");
      showEvent(`You stopped talking · ${formatSeconds(data.audio_seconds)} captured`);
    } else if (phase === "transcript_ready") {
      transcribing = false;
      lastTranscriptAt = eventAt;
      pendingTranscripts += 1;
      setFlowStep("stopped", "done");
      setFlowStep("text", "active");
      lastTranscribeMs =
        Number(data.stop_to_transcript_ms) || (lastStoppedAt ? Math.max(0, eventAt - lastStoppedAt) : 0);
      showEvent(
        lastTranscribeMs
          ? `Transcript ready · ${formatDuration(lastTranscribeMs)} after stop`
          : `Transcript ready · stop timing unavailable`,
      );
    } else if (phase === "no_speech" || phase === "utterance_rejected") {
      transcribing = false;
      setFlowStep("stopped", "done");
      setFlowStep("text", "error");
      showEvent(phase === "no_speech" ? "No speech recognized" : "Not enough voiced audio");
    } else if (phase === "utterance_dropped") {
      transcribing = false;
      setFlowStep("text", "error");
      showEvent("STT backlog dropped an utterance");
    } else if (phase === "transcription_failed") {
      transcribing = false;
      setFlowStep("text", "error");
      showEvent("Transcription failed");
    } else if (phase === "audio_stalled") {
      fault = "NO AUDIO";
      setFlowStep("heard", "error");
      showEvent(`Hardware mic produced no audio for ${formatSeconds(data.empty_seconds)}`);
    } else if (phase === "audio_resumed") {
      fault = "";
      clearFlowError("heard");
      showEvent(`Hardware mic resumed after ${formatDuration(data.stalled_ms)}`);
    } else if (phase === "connection_lost") {
      fault = "STT OFFLINE";
      setFlowStep("text", "error");
      showEvent("Transcription connection lost");
    } else if (phase === "connection_restored") {
      fault = "";
      clearFlowError("text");
      showEvent("Transcription connection restored");
    } else if (phase === "ducking_started") {
      ducking = true;
      showEvent("Mic muted while MARS speaks");
    } else if (phase === "ducking_ended") {
      ducking = false;
      showEvent(`Mic resumed after ${formatDuration(data.duration_ms)}`);
    } else if (phase === "speech_started" && data.source === "tts") {
      showEvent("MARS is generating speech");
    } else if (phase === "audio_started" && data.source === "tts") {
      if (lastTranscriptAt && voiceStartedForTranscriptAt === lastTranscriptAt) return;
      voiceStartedForTranscriptAt = lastTranscriptAt;
      responseTranscriptCount = claimResponseTranscripts(
        pendingTranscripts,
        responseTranscriptCount,
      );
      pendingTranscripts = 0;
      transcriptToVoiceMs = lastTranscriptAt ? Math.max(0, eventAt - lastTranscriptAt) : 0;
      stopToVoiceMs = lastStoppedAt ? Math.max(0, eventAt - lastStoppedAt) : 0;
      setFlowStep("text", "done");
      setFlowStep("reply", "done");
      setFlowStep("voice", "active");
      showEvent(`MARS started talking · ${formatDuration(transcriptToVoiceMs)} after transcript`);
    } else if (phase === "speech_completed" && data.source === "tts") {
      setFlowStep("voice", "done");
      showEvent(`MARS spoke for ${formatSeconds(data.playback_seconds)}`);
    } else if (phase === "speech_failed" && data.source === "tts") {
      setFlowStep("voice", "error");
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
  const unsubscribeChat = ros.subscribe(
    CHAT_OUT_TOPIC,
    (/** @type {any} */ msg) => {
      if (typeof msg?.data !== "string") return;
      try {
        const data = JSON.parse(msg.data);
        if (
          data?.sender !== "robot" ||
          !lastTranscriptAt ||
          replyReportedForTranscriptAt === lastTranscriptAt
        ) {
          return;
        }
        replyReportedForTranscriptAt = lastTranscriptAt;
        const responseAt = eventTime(data);
        transcriptToReplyMs = Math.max(0, responseAt - lastTranscriptAt);
        responseTranscriptCount = claimResponseTranscripts(
          pendingTranscripts,
          responseTranscriptCount,
        );
        pendingTranscripts = 0;
        setFlowStep("text", "done");
        setFlowStep(
          "reply",
          lastTranscriptAt && voiceStartedForTranscriptAt === lastTranscriptAt ? "done" : "active",
        );
        showEvent(`MARS response ready · ${formatDuration(transcriptToReplyMs)} after transcript`);
        refreshStats();
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
      unsubscribeChat();
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

/** @param {any} value */
function eventTime(value) {
  const timestamp = Number(value?.timestamp);
  return Number.isFinite(timestamp) && timestamp > 0 ? timestamp * 1_000 : Date.now();
}

/** @param {number} pending @param {number} current */
function claimResponseTranscripts(pending, current) {
  return pending > 0 ? pending : current;
}
