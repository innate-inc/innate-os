export interface SimulationClock { t: number; receivedAtMs: number }

const WINDOW_MS = 3000;
// Headroom under a 30 Hz display and ordinary rAF scheduling jitter.
const MIN_FPS = 24;
const MIN_SIM_SPEED = 0.8;
// A stalled stream is a connectivity problem, not slow physics, so a window that
// saw one gets no simulation-speed verdict. Shorter gaps are tolerated because
// they cannot by themselves drag a window under MIN_SIM_SPEED.
const STALE_MS = WINDOW_MS * (1 - MIN_SIM_SPEED);

/** Warns after two consecutive 3 s windows that rendered under 24 fps or
 * advanced the simulation clock under 0.8x real time, so an isolated hitch or
 * a brief dip never does. A window whose state stream stalled is judged on
 * frame rate alone. An inactive view or a clock rollback restarts the
 * measurement; the caller must reset() around frame gaps it alone can see (a
 * hidden tab, a parked stage). */
export class SlowdownDetector {
  private start: { now: number; t: number } | null = null;
  private lastT = 0;
  private frames = 0;
  private stalled = false;
  private slowWindows = 0;

  reset(): void {
    this.start = null;
    this.frames = 0;
    this.stalled = false;
    this.slowWindows = 0;
  }

  sample(now: number, clock: SimulationClock | null, active: boolean): boolean {
    if (!active || !clock) {
      this.reset();
      return false;
    }
    const rolledBack = clock.t < this.lastT;
    this.lastT = clock.t;
    if (!this.start || rolledBack) {
      this.reset();
      this.start = { now, t: clock.t };
      return false;
    }
    this.frames++;
    this.stalled ||= now - clock.receivedAtMs > STALE_MS;
    const elapsed = now - this.start.now;
    if (elapsed < WINDOW_MS) return false;
    const seconds = elapsed / 1000;
    const slow =
      this.frames / seconds < MIN_FPS || (!this.stalled && (clock.t - this.start.t) / seconds < MIN_SIM_SPEED);
    this.slowWindows = slow ? this.slowWindows + 1 : 0;
    this.start = { now, t: clock.t };
    this.frames = 0;
    this.stalled = false;
    return this.slowWindows >= 2;
  }
}
