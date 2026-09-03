// @ts-check
// Gap-resistant scheduler for the simulator's short mono PCM chunks.

const LEAD_S = 0.12;
const MAX_QUEUED_S = 0.5;

export class PcmAudioPlayer {
  /** @param {AudioContext} context */
  constructor(context) {
    this.context = context;
    this.nextAt = 0;
    /** @type {Set<AudioBufferSourceNode>} */
    this.sources = new Set();
  }

  /** @param {{ sample_rate?: unknown, pcm?: unknown }} payload */
  push(payload) {
    const rate = Number(payload?.sample_rate);
    if (!Number.isFinite(rate) || rate < 8_000 || rate > 192_000 || typeof payload?.pcm !== "string") return false;

    let bytes;
    try {
      const raw = atob(payload.pcm);
      if (!raw.length || raw.length % 2) return false;
      bytes = new Uint8Array(raw.length);
      for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    } catch {
      return false;
    }

    const samples = new Int16Array(bytes.buffer);
    const buffer = this.context.createBuffer(1, samples.length, rate);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < samples.length; i++) channel[i] = samples[i] / 32768;

    const now = this.context.currentTime;
    if (this.nextAt > now + MAX_QUEUED_S) return false;
    if (this.nextAt < now) this.nextAt = now + LEAD_S;
    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.context.destination);
    this.sources.add(source);
    source.onended = () => this.sources.delete(source);
    source.start(this.nextAt);
    this.nextAt += buffer.duration;
    return true;
  }

  /** Stop queued sound immediately; used when a tab is backgrounded. */
  reset() {
    for (const source of this.sources) {
      try {
        source.stop();
      } catch {
        // It may have ended between the Set iteration and stop().
      }
    }
    this.sources.clear();
    this.nextAt = 0;
  }
}
