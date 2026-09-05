export interface SimulationClock { t: number; worldEpoch: number }

/** Two consecutive 3s windows: ignore isolated hitches, resets and inactive views. */
export class SlowdownDetector {
  private start: { now: number; clock: SimulationClock } | null = null;
  private frames = 0;
  private slowWindows = 0;

  reset(): void {
    this.start = null;
    this.frames = this.slowWindows = 0;
  }

  sample(now: number, clock: SimulationClock | null, active: boolean): boolean {
    if (!active || !clock) { this.reset(); return false; }
    if (!this.start || clock.worldEpoch !== this.start.clock.worldEpoch || clock.t < this.start.clock.t) {
      this.reset();
      this.start = { now, clock };
      return false;
    }
    this.frames++;
    const seconds = (now - this.start.now) / 1000;
    if (seconds < 3) return false;
    const slow = this.frames / seconds < 30 || (clock.t - this.start.clock.t) / seconds < 0.8;
    this.slowWindows = slow ? this.slowWindows + 1 : 0;
    this.start = { now, clock };
    this.frames = 0;
    return this.slowWindows >= 2;
  }
}
