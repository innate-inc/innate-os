// Bounded-concurrency loader: cap in-flight fetches so the big Sala room can't
// starve the small ones, and we don't thrash the keep-alive-less sim server
// (every request is a fresh TLS handshake) -- while still using enough
// parallelism to fill the pipe. Jobs run FIFO, so enqueuing the robot before
// the rooms gives the robot the first slots. Byte progress from every job is
// aggregated into one overall figure for a single loading bar.
//
// The three/DOM imports below are type-only (erased at runtime), so LoadQueue
// has no browser-only dependencies.

import type { GLTF, GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import type { Group } from "three";

export interface LoadProgress {
  /** Bytes downloaded so far, summed across every tracked job. */
  loaded: number;
  /** Best current estimate of the total: max(seeded estimate, sum of known sizes, loaded). */
  total: number;
}

/** Feed one job's download progress: bytes so far and, when known, its size. */
export type ByteReport = (loaded: number, total: number) => void;

export class LoadQueue {
  readonly #limit: number;
  readonly #onProgress?: (p: LoadProgress) => void;
  #active = 0;
  #pending: Array<{ run: () => void; reject: (error: Error) => void }> = [];
  #loaded = 0;
  #estimated = 0; // seeded denominator, before any Content-Length is known
  #nextId = 0;
  #cancelled = false;
  #jobLoaded = new Map<number, number>(); // last loaded bytes reported, per job
  #jobTotal = new Map<number, number>(); // real size once its Content-Length lands

  constructor(limit = 2, onProgress?: (p: LoadProgress) => void) {
    this.#limit = Math.max(1, limit);
    this.#onProgress = onProgress;
  }

  /** True after teardown. Active network requests may still finish, but none
   * of their queue promises or progress reports can escape after this point. */
  get cancelled(): boolean {
    return this.#cancelled;
  }

  /** Seed the denominator so the bar has a width before any bytes arrive. */
  setEstimatedTotal(bytes: number): void {
    this.#estimated = bytes;
    this.#emit();
  }

  /**
   * Enqueue a job; it runs once a slot frees (≤ limit at a time), FIFO. `job`
   * gets a reporter to feed download progress; each enqueue gets its own id, so
   * repeated reports (loaders fire onProgress many times) accumulate correctly
   * even when two jobs fetch the same URL.
   */
  add<T>(job: (report: ByteReport) => Promise<T>): Promise<T> {
    if (this.#cancelled) return Promise.reject(new Error("load queue cancelled"));
    const id = this.#nextId++;
    return new Promise<T>((resolve, reject) => {
      const run = () => {
        if (this.#cancelled) {
          reject(new Error("load queue cancelled"));
          return;
        }
        this.#active++;
        const report: ByteReport = (loaded, total) => this.#report(id, loaded, total);
        // Promise.resolve().then guards a job that throws *synchronously* -- it
        // becomes a rejection, so the slot is still released in finally.
        Promise.resolve()
          .then(() => {
            if (this.#cancelled) throw new Error("load queue cancelled");
            return job(report);
          })
          .then(
            (value) => {
              if (this.#cancelled) reject(new Error("load queue cancelled"));
              else resolve(value);
            },
            reject,
          )
          .finally(() => {
            this.#active--;
            if (!this.#cancelled) this.#pending.shift()?.run();
          });
      };
      if (this.#active < this.#limit) run();
      else this.#pending.push({ run, reject });
    });
  }

  /** Reject queued consumers and stop starting downloads. Active browser
   * requests may finish, but their results and progress are ignored. */
  cancel(): void {
    if (this.#cancelled) return;
    this.#cancelled = true;
    const error = new Error("load queue cancelled");
    const pending = this.#pending.splice(0);
    for (const job of pending) job.reject(error);
  }

  #report(id: number, loaded: number, total: number): void {
    if (this.#cancelled) return;
    const prev = this.#jobLoaded.get(id) ?? 0;
    this.#loaded += loaded - prev;
    this.#jobLoaded.set(id, loaded);
    if (total > 0) this.#jobTotal.set(id, total);
    this.#emit();
  }

  #emit(): void {
    let known = 0;
    for (const t of this.#jobTotal.values()) known += t;
    const total = Math.max(this.#estimated, known, this.#loaded);
    this.#onProgress?.({ loaded: this.#loaded, total });
  }
}

/** Load a GLB through the queue, forwarding its byte progress. Resolves the
 * root Object3D (gltf.scene). The server sends Content-Length, so ev.total is
 * a real size, not 0. */
export function queuedGLB(queue: LoadQueue, loader: GLTFLoader, url: string): Promise<Group> {
  return queue.add(
    (report) =>
      new Promise<Group>((resolve, reject) =>
        loader.load(
          url,
          (gltf: GLTF) => resolve(gltf.scene),
          (ev: ProgressEvent) => report(ev.loaded, ev.total),
          reject,
        ),
      ),
  );
}
