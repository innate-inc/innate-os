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
    <div class="audio-debug-signals mono">
      <div><span>ROLLING RMS</span><strong class="audio-debug-rms">—</strong></div>
      <div><span>SILERO SCORE</span><strong class="audio-debug-silero">—</strong></div>
      <div><span>DUCKING</span><strong class="audio-debug-ducking">—</strong></div>
      <div><span>CAPTURE</span><strong class="audio-debug-device">—</strong></div>
    </div>
    <div class="audio-debug-trace">
      <div class="audio-debug-trace-head mono">
        <span>LAST 10 SECONDS</span>
        <span class="audio-debug-trace-key"><i data-signal="rms"></i>RMS <i data-signal="silero"></i>SILERO</span>
      </div>
      <svg viewBox="0 0 300 64" preserveAspectRatio="none" aria-label="Rolling RMS and Silero history">
        <line class="audio-debug-trace-mid" x1="0" y1="32" x2="300" y2="32"></line>
        <line class="audio-debug-trace-trigger" x1="0" y1="64" x2="300" y2="64"></line>
        <polyline class="audio-debug-trace-rms" points=""></polyline>
        <polyline class="audio-debug-trace-silero" points=""></polyline>
      </svg>
    </div>
    <div class="audio-debug-flow mono" aria-label="Speech pipeline">
      <span data-step="heard">HEARD</span>
      <span data-step="stopped">STOP</span>
      <span data-step="text">TEXT</span>
      <span data-step="reply">REPLY</span>
      <span data-step="voice">VOICE</span>
    </div>
    <div class="audio-debug-event">Waiting for microphone telemetry…</div>
    <div class="audio-debug-failure mono" hidden>
      <span>LAST UNRECOGNIZED</span>
      <strong class="audio-debug-failure-summary"></strong>
    </div>
    <div class="audio-debug-stats mono"></div>
  `;
  stage.append(root);

  const state = /** @type {HTMLElement} */ (root.querySelector(".audio-debug-state"));
  const meter = /** @type {HTMLElement} */ (root.querySelector(".audio-debug-meter"));
  const level = /** @type {HTMLElement} */ (root.querySelector(".audio-debug-level"));
  const threshold = /** @type {HTMLElement} */ (root.querySelector(".audio-debug-threshold"));
  const engine = /** @type {HTMLElement} */ (root.querySelector(".audio-debug-engine"));
  const reading = /** @type {HTMLElement} */ (root.querySelector(".audio-debug-reading"));
  const rmsReading = /** @type {HTMLElement} */ (root.querySelector(".audio-debug-rms"));
  const sileroReading = /** @type {HTMLElement} */ (root.querySelector(".audio-debug-silero"));
  const duckingReading = /** @type {HTMLElement} */ (root.querySelector(".audio-debug-ducking"));
  const deviceReading = /** @type {HTMLElement} */ (root.querySelector(".audio-debug-device"));
  const rmsTrace = /** @type {SVGPolylineElement} */ (root.querySelector(".audio-debug-trace-rms"));
  const sileroTrace = /** @type {SVGPolylineElement} */ (root.querySelector(".audio-debug-trace-silero"));
  const triggerTrace = /** @type {SVGLineElement} */ (root.querySelector(".audio-debug-trace-trigger"));
  const event = /** @type {HTMLElement} */ (root.querySelector(".audio-debug-event"));
  const failure = /** @type {HTMLElement} */ (root.querySelector(".audio-debug-failure"));
  const failureSummary = /** @type {HTMLElement} */ (root.querySelector(".audio-debug-failure-summary"));
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
  let lastRms = 0;
  let lastSilero = 0;
  let sileroPaused = false;
  let audioDeviceId = "";
  let audioDeviceName = "";
  let capture = "";
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
  /** @type {{ rms: number, silero: number }[]} */
  const signalHistory = [];
  /** @type {{ rms: number, silero: number }[]} */
  let utteranceSignals = [];
  let collectingUtterance = false;

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

  function refreshDiagnostics() {
    rmsReading.textContent = lastRms.toFixed(4);
    sileroReading.textContent = vendorVad
      ? "VENDOR"
      : lastEngine !== "silero"
        ? "NOT ACTIVE"
      : sileroPaused
        ? `${lastSilero.toFixed(3)} PAUSED`
        : lastSilero.toFixed(3);
    duckingReading.textContent = ducking ? "ACTIVE · AUDIO DISCARDED" : "OFF";
    duckingReading.dataset.active = String(ducking);
    deviceReading.textContent = audioDeviceName
      ? `${audioDeviceName} · ${audioDeviceId}`
      : audioDeviceId || capture || "UNKNOWN";
    const rmsScale = Math.max(0.02, ...signalHistory.map((sample) => sample.rms * 1.15));
    rmsTrace.setAttribute("points", tracePoints(signalHistory, (sample) => sample.rms / rmsScale, 1, 30));
    sileroTrace.setAttribute("points", tracePoints(signalHistory, (sample) => sample.silero, 33, 30));
    triggerTrace.setAttribute("y1", String(63 - Math.min(1, lastThreshold) * 30));
    triggerTrace.setAttribute("y2", String(63 - Math.min(1, lastThreshold) * 30));
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

  /** @param {any} data @param {string} phase */
  function showUnrecognized(data, phase) {
    const samples =
      utteranceSignals.length > 0
        ? utteranceSignals
        : [{ rms: Number(data.rms) || lastRms, silero: Number(data.silero_score) || lastSilero }];
    const rmsAverage = samples.reduce((sum, sample) => sum + sample.rms, 0) / samples.length;
    const rmsPeak = Math.max(...samples.map((sample) => sample.rms));
    const sileroPeak = Math.max(...samples.map((sample) => sample.silero));
    const clipSeconds = Number(data.audio_seconds);
    const captured = Number.isFinite(clipSeconds) ? `${clipSeconds.toFixed(2)}s clip` : "clip length unknown";
    const reason = phase === "no_speech" ? "STT returned no text" : "local VAD rejected clip";
    failureSummary.textContent = [
      new Date().toLocaleTimeString(),
      reason,
      captured,
      `RMS avg ${rmsAverage.toFixed(4)} / peak ${rmsPeak.toFixed(4)}`,
      `Silero peak ${sileroPeak.toFixed(3)} / trigger ${lastThreshold.toFixed(3)}`,
      ducking ? "ducking active" : "ducking off",
      audioDeviceId || capture || "capture unknown",
    ].join(" · ");
    failure.hidden = false;
  }

  /** @param {any} data */
  function onVad(data) {
    markFresh();
    lastBackend = String(data.backend ?? "");
    lastEngine = String(data.engine ?? "");
    vendorVad = lastEngine === "vendor";
    lastLevel = Number(data.level) || 0;
    lastRms = Number(data.rms) || 0;
    lastSilero = Number(data.silero_score) || 0;
    sileroPaused = Boolean(data.silero_paused);
    audioDeviceId = String(data.audio_device_id ?? "");
    audioDeviceName = String(data.audio_device_name ?? "");
    capture = String(data.capture ?? "");
    lastThreshold = Number(data.threshold) || 0;
    voiced = Boolean(data.voiced);
    utteranceOpen = Boolean(data.utterance_open);
    utteranceSeconds = Number(data.utterance_secs) || 0;
    ducking = Boolean(data.ducking);
    if (!vendorVad) {
      const sample = { rms: lastRms, silero: lastSilero };
      signalHistory.push(sample);
      if (signalHistory.length > 50) signalHistory.shift();
      if (collectingUtterance) utteranceSignals.push(sample);
    }
    refreshState();
    refreshMeter();
    refreshDiagnostics();
    refreshStats();
  }

  /** @param {any} data */
  function onDebug(data) {
    markFresh();
    lastBackend = String(data.backend ?? lastBackend);
    lastEngine = String(data.engine ?? lastEngine);
    lastRms = Number(data.rms ?? lastRms) || 0;
    lastSilero = Number(data.silero_score ?? lastSilero) || 0;
    sileroPaused = Boolean(data.silero_paused ?? sileroPaused);
    audioDeviceId = String(data.audio_device_id ?? audioDeviceId);
    audioDeviceName = String(data.audio_device_name ?? audioDeviceName);
    capture = String(data.capture ?? capture);
    ducking = Boolean(data.ducking ?? ducking);
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
      collectingUtterance = true;
      utteranceSignals = [];
      lastStoppedAt = 0;
      resetFlow();
      setFlowStep("heard", "active");
      showEvent("You are talking");
    } else if (phase === "utterance_closed") {
      transcribing = true;
      voiced = false;
      utteranceOpen = false;
      collectingUtterance = false;
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
      collectingUtterance = false;
      setFlowStep("stopped", "done");
      setFlowStep("text", "error");
      showEvent(phase === "no_speech" ? "No speech recognized" : "Not enough voiced audio");
      showUnrecognized(data, phase);
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
    refreshDiagnostics();
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
  refreshDiagnostics();
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

/**
 * @param {{ rms: number, silero: number }[]} samples
 * @param {(sample: { rms: number, silero: number }) => number} value
 * @param {number} top
 * @param {number} height
 */
function tracePoints(samples, value, top, height) {
  if (samples.length === 0) return "";
  const denominator = Math.max(1, samples.length - 1);
  return samples
    .map((sample, index) => {
      const x = (index / denominator) * 300;
      const y = top + (1 - Math.min(1, Math.max(0, value(sample)))) * height;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
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
