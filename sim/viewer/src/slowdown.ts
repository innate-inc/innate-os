export interface SimulationClock { t: number; receivedAt: number }

const WINDOW_MS = 3000;
// Headroom under a 30 Hz display and ordinary rAF scheduling jitter.
const MIN_FPS = 24;
const MIN_SIM_SPEED = 0.8;
// A stalled world stream is a connectivity problem, not slow physics.
const STALE_MS = 500;

/** Warns after two consecutive 3 s windows that rendered under 24 fps or
 * advanced the simulation clock under 0.8x real time, so an isolated hitch or
 * a brief dip never does. An inactive view, a stale stream or a clock rollback
 * restarts the measurement; the caller must reset() around frame gaps it alone
 * can see (a hidden tab, a parked stage). */
export class SlowdownDetector {
  private start: { now: number; t: number } | null = null;
  private lastT = 0;
  private frames = 0;
  private slowWindows = 0;

  reset(): void {
    this.start = null;
    this.slowWindows = 0;
  }

  sample(now: number, clock: SimulationClock | null, active: boolean): boolean {
    if (!active || !clock || now - clock.receivedAt > STALE_MS) {
      this.reset();
      return false;
    }
    const rolledBack = clock.t < this.lastT;
    this.lastT = clock.t;
    if (!this.start || rolledBack) {
      this.reset();
      this.start = { now, t: clock.t };
      this.frames = 0;
      return false;
    }
    this.frames++;
    const elapsed = now - this.start.now;
    if (elapsed < WINDOW_MS) return false;
    const seconds = elapsed / 1000;
    const slow = this.frames / seconds < MIN_FPS || (clock.t - this.start.t) / seconds < MIN_SIM_SPEED;
    this.slowWindows = slow ? this.slowWindows + 1 : 0;
    this.start = { now, t: clock.t };
    this.frames = 0;
    return this.slowWindows >= 2;
  }
}
