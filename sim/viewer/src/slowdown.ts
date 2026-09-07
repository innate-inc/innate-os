export interface SimulationClock { t: number; receivedAt: number }

const WINDOW_MS = 6000;
// Headroom under a 30 Hz display and ordinary rAF scheduling jitter.
const MIN_FPS = 24;
const MIN_SIM_SPEED = 0.8;
// A stalled world stream is a connectivity problem, not slow physics.
const STALE_MS = 500;
// No rendering loop leaves a gap this long: a hidden tab, a parked stage or a stall.
const GAP_MS = 1000;

/** True once 6 s of uninterrupted frames averaged under 24 fps or advanced the
 * simulation clock under 0.8x real time. An inactive view, a stale stream, a
 * clock rollback or a gap between frames restarts the window instead. */
export class SlowdownDetector {
  private start: { now: number; t: number } | null = null;
  private lastNow = -Infinity;
  private frames = 0;

  sample(now: number, clock: SimulationClock | null, active: boolean): boolean {
    const gap = now - this.lastNow;
    this.lastNow = now;
    if (!active || !clock || now - clock.receivedAt > STALE_MS) {
      this.start = null;
      return false;
    }
    if (!this.start || gap > GAP_MS || clock.t < this.start.t) {
      this.start = { now, t: clock.t };
      this.frames = 0;
      return false;
    }
    this.frames++;
    const elapsed = now - this.start.now;
    if (elapsed < WINDOW_MS) return false;
    const seconds = elapsed / 1000;
    const slow = this.frames / seconds < MIN_FPS || (clock.t - this.start.t) / seconds < MIN_SIM_SPEED;
    this.start = { now, t: clock.t };
    this.frames = 0;
    return slow;
  }
}
