// @ts-check

/** @typedef {{ stoppedAt: number | null, transcriptAt: number | null, replied: boolean, voiced: boolean }} SpeechRecord */

export function createSpeechTimeline() {
  /** @type {Map<string, SpeechRecord>} */
  const records = new Map();

  /** @param {string} utteranceId */
  function recordFor(utteranceId) {
    let record = records.get(utteranceId);
    if (record !== undefined) return record;
    record = { stoppedAt: null, transcriptAt: null, replied: false, voiced: false };
    records.set(utteranceId, record);
    const oldest = records.keys().next().value;
    if (records.size > 32 && oldest !== undefined) records.delete(oldest);
    return record;
  }

  return {
    /** @param {any} raw */
    observe(raw) {
      const event = { ...raw };
      const utteranceId = eventUtteranceId(event);
      if (utteranceId === null) return event;
      const record = recordFor(utteranceId);
      const at = timestampMs(event.timestamp);
      if (event.source === "stt" && event.phase === "speech_started") {
        event.new_exchange = true;
        record.stoppedAt = null;
      } else if (event.source === "stt" && event.phase === "utterance_closed") {
        record.stoppedAt = at;
      } else if (event.source === "stt" && event.phase === "transcript_ready") {
        event.stop_to_transcript_ms ??=
          record.stoppedAt === null ? null : Math.max(0, at - record.stoppedAt);
        record.transcriptAt = at;
      } else if (event.source === "tts" && event.phase === "audio_started") {
        if (record.voiced) return null;
        record.voiced = true;
        event.transcript_to_voice_ms =
          record.transcriptAt === null ? null : Math.max(0, at - record.transcriptAt);
        event.stop_to_voice_ms =
          record.stoppedAt === null ? null : Math.max(0, at - record.stoppedAt);
        event.bundled_transcripts = bundledTranscriptCount(event);
      }
      return event;
    },

    /** @param {any} raw */
    response(raw) {
      const utteranceId = eventUtteranceId(raw);
      if (utteranceId === null) return null;
      const record = records.get(utteranceId);
      if (record?.transcriptAt == null || record.replied) return null;
      record.replied = true;
      const at = timestampMs(raw.timestamp);
      return {
        source: "agent",
        phase: "response_ready",
        utterance_id: utteranceId,
        utterance_ids: raw.utterance_ids,
        timestamp: at / 1000,
        transcript_to_response_ms: Math.max(0, at - record.transcriptAt),
        stop_to_response_ms:
          record.stoppedAt === null ? null : Math.max(0, at - record.stoppedAt),
        bundled_transcripts: bundledTranscriptCount(raw),
        voice_started: record.voiced,
      };
    },

    reset() {
      records.clear();
    },
  };
}

/** @param {any} event @returns {{ title: string, detail: string, level: string } | null} */
export function describeDebugEvent(event) {
  const source = String(event?.source ?? "");
  const phase = String(event?.phase ?? "");
  const backend = [event?.backend, event?.engine].filter(Boolean).join(" / ");
  if (source === "stt" && phase === "transcript_ready") {
    const stopped = event?.stop_to_transcript_ms != null;
    return {
      title: stopped
        ? `Transcript ready ${duration(event.stop_to_transcript_ms)} after you stopped`
        : `Transcript ready ${duration(event?.total_ms)} after speech detection`,
      detail: joinDetails(
        event?.audio_seconds != null ? `${seconds(event.audio_seconds)} audio` : "",
        event?.queue_ms != null ? `${duration(event.queue_ms)} queued` : "",
        event?.transcribe_ms != null ? `${duration(event.transcribe_ms)} API` : "",
        event?.characters != null ? `${event.characters} characters` : "",
        vadDetail(event),
        event?.audio_queue_chunks ? `${event.audio_queue_chunks} audio chunks buffered` : "",
        event?.dropped_audio_chunks ? `${event.dropped_audio_chunks} audio chunks dropped` : "",
        event?.capture,
        backend,
      ),
      level: "success",
    };
  }
  if (source === "stt" && phase === "no_speech") {
    return {
      title: `No speech recognized after ${duration(event?.stop_to_transcript_ms ?? event?.total_ms)}`,
      detail: joinDetails(
        event?.audio_seconds != null ? `${seconds(event.audio_seconds)} audio` : "",
        vadDetail(event),
        audioDiagnosticDetail(event),
        captureDetail(event),
        backend,
      ),
      level: "warning",
    };
  }
  if (source === "stt" && phase === "utterance_rejected") {
    return {
      title: "Not enough voiced audio to transcribe",
      detail: joinDetails(
        event?.audio_seconds != null ? `${seconds(event.audio_seconds)} audio` : "",
        closeReason(event),
        vadDetail(event),
        audioDiagnosticDetail(event),
        captureDetail(event),
        backend,
      ),
      level: "warning",
    };
  }
  if (source === "stt" && phase === "utterance_dropped") {
    return {
      title: "Dropped an utterance because transcription was backlogged",
      detail: event?.audio_seconds != null ? `${seconds(event.audio_seconds)} audio` : backend,
      level: "error",
    };
  }
  if (source === "stt" && phase === "transcription_failed") {
    return {
      title: `Transcription failed after ${duration(event?.transcribe_ms)}`,
      detail: joinDetails(String(event?.error ?? ""), backend),
      level: "error",
    };
  }
  if (source === "stt" && phase === "audio_stalled") {
    return {
      title: `Microphone capture produced no audio for ${seconds(event?.empty_seconds)}`,
      detail: joinDetails(event?.capture, backend),
      level: "error",
    };
  }
  if (source === "stt" && phase === "audio_resumed") {
    return {
      title: `Microphone audio resumed after ${duration(event?.stalled_ms)}`,
      detail: joinDetails(event?.capture, backend),
      level: "success",
    };
  }
  if (source === "stt" && phase === "connection_lost") {
    return {
      title: "Realtime transcription connection lost",
      detail: joinDetails("reconnecting automatically", backend),
      level: "error",
    };
  }
  if (source === "stt" && phase === "connection_restored") {
    return {
      title: "Realtime transcription connection restored",
      detail: backend,
      level: "success",
    };
  }
  if (source === "tts" && phase === "audio_started") {
    const transcriptLatency = event?.transcript_to_voice_ms;
    return {
      title:
        transcriptLatency == null
          ? "MARS started talking"
          : `MARS started talking ${duration(transcriptLatency)} after transcript`,
      detail: joinDetails(
        event?.stop_to_voice_ms ? `${duration(event.stop_to_voice_ms)} stop → voice` : "",
        event?.ttfb_ms != null ? `${duration(event.ttfb_ms)} TTS first byte` : "",
        event?.bundled_transcripts > 1 ? `${event.bundled_transcripts} transcripts bundled` : "",
        String(event?.output ?? ""),
      ),
      level: "success",
    };
  }
  if (source === "tts" && phase === "speech_completed") {
    return {
      title: `MARS finished ${seconds(event?.playback_seconds)} of speech`,
      detail: joinDetails(
        event?.ttfb_ms != null ? `${duration(event.ttfb_ms)} to first audio` : "",
        event?.stream_ms != null ? `${duration(event.stream_ms)} generation` : "",
        event?.total_ms != null ? `${duration(event.total_ms)} total` : "",
        String(event?.output ?? ""),
      ),
      level: "success",
    };
  }
  if (source === "tts" && phase === "speech_failed") {
    return {
      title: `MARS speech failed after ${duration(event?.total_ms)}`,
      detail: event?.characters != null ? `${event.characters} characters` : "",
      level: "error",
    };
  }
  if (source === "agent" && phase === "response_ready") {
    const transcriptLatency = event?.transcript_to_response_ms;
    return {
      title:
        transcriptLatency == null
          ? "MARS response ready"
          : `MARS response ready ${duration(transcriptLatency)} after transcript`,
      detail: joinDetails(
        event?.stop_to_response_ms ? `${duration(event.stop_to_response_ms)} stop → response` : "",
        event?.bundled_transcripts > 1 ? `${event.bundled_transcripts} transcripts bundled` : "",
      ),
      level: "success",
    };
  }
  return null;
}

/** @param {unknown} value */
function duration(value) {
  if (value == null) return "unknown time";
  const ms = Number(value);
  if (!Number.isFinite(ms)) return "unknown time";
  return ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)}s`;
}

/** @param {unknown} value */
function seconds(value) {
  if (value == null) return "an unknown duration";
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return "an unknown duration";
  return `${seconds.toFixed(seconds < 10 ? 2 : 1)}s`;
}

/** @param {...string} values */
function joinDetails(...values) {
  return values.filter(Boolean).join(" · ");
}

/** @param {unknown} value */
export function timestampMs(value) {
  const seconds = Number(value);
  return Number.isFinite(seconds) && seconds > 0 ? seconds * 1000 : Date.now();
}

/** @param {any} event */
function eventUtteranceId(event) {
  return typeof event?.utterance_id === "string" ? event.utterance_id : null;
}

/** @param {any} event */
function bundledTranscriptCount(event) {
  return Array.isArray(event?.utterance_ids) && event.utterance_ids.length > 0
    ? event.utterance_ids.length
    : 1;
}

/** @param {any} event */
function vadDetail(event) {
  const peak = Number(event?.peak_level);
  const current = Number(event?.vad_level);
  const threshold = Number(event?.vad_threshold);
  const level = Number.isFinite(peak) ? peak : current;
  if (!Number.isFinite(level) || !Number.isFinite(threshold)) return "";
  const label = Number.isFinite(peak) ? "VAD peak" : "VAD";
  return `${label} ${level.toFixed(3)} / ${threshold.toFixed(3)} trigger`;
}

/** @param {any} event */
function audioDiagnosticDetail(event) {
  const rms = Number(event?.rms);
  const silero = Number(event?.vad_level);
  const values = [];
  if (event?.rms != null && Number.isFinite(rms)) values.push(`rolling RMS ${rms.toFixed(4)}`);
  if (event?.engine === "silero" && event?.vad_level != null && Number.isFinite(silero)) {
    values.push(`Silero ${silero.toFixed(3)}${event?.ducking ? " (paused)" : ""}`);
  }
  values.push(event?.ducking ? "ducking active; audio discarded" : "ducking off");
  return values.join(" · ");
}

/** @param {any} event */
function captureDetail(event) {
  const device = String(event?.audio_device_id ?? "");
  const name = String(event?.audio_device_name ?? "");
  return joinDetails(event?.capture, name, device);
}

/** @param {any} event */
function endpointDetail(event) {
  const silence = Number(event?.silence_seconds);
  return Number.isFinite(silence) ? `${silence.toFixed(2)}s silence endpoint` : "";
}

/** @param {any} event */
function closeReason(event) {
  if (event?.close_reason === "max_duration") return "ended by 30s safety limit";
  if (event?.close_reason === "silence") return joinDetails("ended by silence", endpointDetail(event));
  return endpointDetail(event);
}

/** @param {unknown} value */
export function clockTime(value) {
  const date = new Date(timestampMs(value));
  const millis = String(date.getMilliseconds()).padStart(3, "0");
  const time = date.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
  return `${time}.${millis}`;
}
