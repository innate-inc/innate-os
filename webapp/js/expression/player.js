// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Studio playback: a clip's actuator frames sampled at any time with linear
// interpolation, the animator's crossfade into each clip, a queue, and the
// settle back into idle breathing when the last clip ends or Stop lands — so
// the preview moves the way the robot's animator will. Pure: time is pushed in.

import { ACTUATOR_KEYS } from "./pipeline.js";

/** @typedef {import("./pipeline.js").ActuatorPose} ActuatorPose */
/** @typedef {import("./pipeline.js").Clip} Clip */
/** @typedef {{ clip: Clip, poses: ActuatorPose[] }} Take */

const BLEND_S = 0.4;
const LOOP_REST_S = 0.6;

/** @param {number} u */
const smooth = (u) => (u <= 0 ? 0 : u >= 1 ? 1 : u * u * (3 - 2 * u));

/** @param {ActuatorPose} a @param {ActuatorPose} b @param {number} w @returns {ActuatorPose} */
export function lerpPose(a, b, w) {
  const out = /** @type {ActuatorPose} */ ({ ...a });
  for (const k of ACTUATOR_KEYS) out[k] = a[k] + (b[k] - a[k]) * w;
  out.grip = a.grip + (b.grip - a.grip) * w;
  return out;
}

/** @param {Take} take */
export const takeDuration = (take) => (take.poses.length - 1) / take.clip.fps;

/** @param {Take} take @param {number} t */
export function sampleTake(take, t) {
  const f = Math.max(0, t) * take.clip.fps;
  const i = Math.min(take.poses.length - 1, Math.floor(f));
  const j = Math.min(take.poses.length - 1, i + 1);
  return lerpPose(take.poses[i], take.poses[j], f - i);
}

/** @template {Take} T a take, plus whatever the caller carries on it */
export class Player {
  /**
   * @param {(t: number) => ActuatorPose} idle the pose between clips at clock time t (breathing)
   * @param {(take: T | null) => void} [onTake] fires when the playing take changes
   */
  constructor(idle, onTake) {
    this.idle = idle;
    this.onTake = onTake;
    this.clock = 0;
    /** Between clips (breathing), as opposed to paused inside one. */
    this.idling = true;
    /** @type {T | null} */
    this.take = null;
    /** @type {T[]} */
    this.queue = [];
    this.t = 0;
    this.playing = false;
    this.loop = false;
    /** The pose a new take crossfades from (null after a scrub: show the clip exactly). @type {ActuatorPose | null} */
    this.blendFrom = null;
    /** Easing back into idle once nothing is playing. @type {{ from: ActuatorPose, t: number } | null} */
    this.settle = null;
    /** @type {ActuatorPose} */
    this.pose = idle(0);
  }

  get duration() {
    return this.take ? takeDuration(this.take) : 0;
  }

  /** Play `take` now, crossfading from wherever the body is; anything queued is dropped. @param {T} take */
  play(take) {
    this.queue = [];
    this.#start(take);
  }

  /** Play after everything already playing/queued. @param {T} take */
  enqueue(take) {
    if (!this.take || !this.playing) this.#start(take);
    else this.queue.push(take);
  }

  /** Stop and ease back into idle; the take stays loaded for scrubbing. */
  stop() {
    this.queue = [];
    this.#rest(this.pose);
  }

  toggle() {
    if (!this.take) return;
    if (this.playing) {
      this.playing = false;
      return;
    }
    if (this.idling || this.t >= this.duration) {
      this.t = 0;
      this.blendFrom = this.pose;
    }
    this.settle = null;
    this.idling = false;
    this.playing = true;
  }

  /** Play the loaded take from its first frame, with no crossfade (recordings start clean). */
  restart() {
    if (!this.take) return;
    this.queue = [];
    this.seek(0);
    this.playing = true;
  }

  /** Jump to `t` (paused or not) and show the clip exactly there. @param {number} t */
  seek(t) {
    if (!this.take) return;
    this.t = Math.min(this.duration, Math.max(0, t));
    this.blendFrom = null;
    this.settle = null;
    this.idling = false;
    this.pose = sampleTake(this.take, this.t);
  }

  /** Advance the clock by `dt` wall seconds and return the pose to show. @param {number} dt @returns {ActuatorPose} */
  advance(dt) {
    this.clock += dt;
    if (this.settle) {
      this.settle.t += dt;
      this.pose = lerpPose(this.settle.from, this.idle(this.clock), smooth(this.settle.t / BLEND_S));
      if (this.settle.t >= BLEND_S) this.settle = null;
      return this.pose;
    }
    if (this.idling) {
      this.pose = this.idle(this.clock);
      return this.pose;
    }
    if (!this.take || !this.playing) return this.pose;
    this.t += dt;
    const duration = this.duration;
    if (this.t >= duration) return this.#finish(duration);
    this.pose = this.#blended(this.take, this.t);
    return this.pose;
  }

  /** The take at `t`, still crossfading from where the body was for the first BLEND_S. @param {T} take @param {number} t */
  #blended(take, t) {
    const base = sampleTake(take, t);
    return this.blendFrom ? lerpPose(this.blendFrom, base, smooth(t / BLEND_S)) : base;
  }

  /** @param {number} duration @returns {ActuatorPose} */
  #finish(duration) {
    const end = this.#blended(/** @type {T} */ (this.take), duration);
    const next = this.queue.shift();
    if (next) {
      this.pose = end;
      this.#start(next);
      return this.pose;
    }
    if (this.loop && this.t >= duration + LOOP_REST_S) {
      this.t = 0;
      this.blendFrom = end;
      return end;
    }
    if (this.loop) {
      this.pose = end;
      return end;
    }
    this.t = duration;
    this.#rest(end);
    return this.advance(0);
  }

  /** @param {ActuatorPose} from */
  #rest(from) {
    this.playing = false;
    this.idling = true;
    this.settle = { from, t: 0 };
  }

  /** @param {T} take */
  #start(take) {
    this.blendFrom = this.pose;
    this.settle = null;
    this.idling = false;
    this.take = take;
    this.t = 0;
    this.playing = true;
    this.onTake?.(take);
  }
}
