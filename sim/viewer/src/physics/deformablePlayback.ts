import type { DeformableFrame } from "./deformableFrame";

/** Bounded control-mesh history sampled on the robot's playback clock. */
export class DeformablePlayback {
  #frames = new Map<number, DeformableFrame[]>();
  #outputs = new Map<number, DeformableFrame>();

  clear(): void {
    this.#frames.clear();
    this.#outputs.clear();
  }

  delete(id: number): void {
    this.#frames.delete(id);
    this.#outputs.delete(id);
  }

  push(frame: DeformableFrame): void {
    let frames = this.#frames.get(frame.id);
    const last = frames?.at(-1);
    if (!frames || (last && (frame.t < last.t || frame.vertexCount !== last.vertexCount))) {
      frames = [];
      this.#frames.set(frame.id, frames);
      this.#outputs.delete(frame.id);
    }
    if (frames.at(-1)?.t === frame.t) frames.pop();
    frames.push(frame);
    if (frames.length > 60) frames.shift();
  }

  sample(t: number): DeformableFrame[] {
    const result: DeformableFrame[] = [];
    for (const [id, frames] of this.#frames) {
      while (frames.length > 2 && frames[1].t <= t) frames.shift();
      const a = frames[0];
      const b = frames[1] ?? a;
      const u = b.t > a.t ? Math.min(1, Math.max(0, (t - a.t) / (b.t - a.t))) : 0;
      let out = this.#outputs.get(id);
      if (!out) {
        out = { id, t, vertexCount: a.vertexCount, positions: new Float32Array(a.positions.length) };
        this.#outputs.set(id, out);
      }
      out.t = a.t + (b.t - a.t) * u;
      for (let i = 0; i < out.positions.length; i++) {
        out.positions[i] = a.positions[i] + (b.positions[i] - a.positions[i]) * u;
      }
      result.push(out);
    }
    return result;
  }
}
