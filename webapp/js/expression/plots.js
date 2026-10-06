// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Channel timeline: one strip per plan channel showing the three stages of the
// pipeline the way Binh Pham's figures do — the recipe's raw expansion (faint),
// the sparse plan keys (dashed, dotted), and the generated 25 Hz motion (solid) —
// with a playhead that scrubs on drag. The static layer is cached; only the
// playhead redraws per frame.

import { CHANNELS, DSL_KEYS, ENERGY, FPS, LIMITS } from "./pipeline.js";

/** @typedef {import("./pipeline.js").Plan} Plan */
/** @typedef {{ frames: number[][] | null, plan: Plan | null, motion: number[][], fps: number, duration: number }} Series */

const LABEL_W = 92;
const VALUE_W = 54;
const STRIP_H = 44;
const GAP = 6;
const AXIS_H = 18;
const UNITS = ["", "", "", "", "", "°", "m", "", ""];
const TICK_STEPS = [0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600];

/**
 * @param {HTMLElement} container
 * @param {{ onSeek: (t: number) => void }} hooks
 */
export function createTimeline(container, { onSeek }) {
  const canvas = document.createElement("canvas");
  canvas.className = "exs-timeline-canvas";
  canvas.style.touchAction = "none";
  container.appendChild(canvas);
  const ctx = /** @type {CanvasRenderingContext2D} */ (canvas.getContext("2d"));
  const layer = document.createElement("canvas");
  const lctx = /** @type {CanvasRenderingContext2D} */ (layer.getContext("2d"));

  /** @type {Series | null} */
  let series = null;
  let duration = 0;
  let playhead = 0;
  let width = 0;
  const height = CHANNELS.length * (STRIP_H + GAP) + AXIS_H;
  const css = getComputedStyle(container);
  const color = (/** @type {string} */ name, /** @type {string} */ fallback) =>
    css.getPropertyValue(name).trim() || fallback;
  const C = {
    text: color("--text", "#e7e7ea"),
    muted: color("--muted", "#8a8a93"),
    hair: color("--hairline", "rgb(255 255 255 / 8%)"),
    accent: color("--accent", "#e8a33d"),
    plan: color("--type-replay", "#6b93d6"),
  };

  const plotW = () => Math.max(10, width - LABEL_W - VALUE_W);
  const xOf = (/** @type {number} */ t) => LABEL_W + (duration ? (t / duration) * plotW() : 0);

  /** Value range drawn for channel j (energy auto-scales to the clip). @param {number} j */
  function range(j) {
    if (j !== ENERGY) return LIMITS[DSL_KEYS[j]];
    const peak = Math.max(3, ...(series?.plan?.keys.map((k) => k.energy) ?? [0]));
    return /** @type {[number, number]} */ ([0, Math.ceil(peak)]);
  }

  /** @param {number} j @param {number} v */
  function yOf(j, v) {
    const [lo, hi] = range(j);
    const top = j * (STRIP_H + GAP) + 3;
    const u = (Math.min(hi, Math.max(lo, v)) - lo) / (hi - lo);
    return top + (1 - u) * (STRIP_H - 6);
  }

  /** @param {CanvasRenderingContext2D} g @param {number} j @param {number[]} ts @param {number[]} vs */
  function path(g, j, ts, vs) {
    g.beginPath();
    ts.forEach((t, i) => {
      const x = xOf(t);
      const y = yOf(j, vs[i]);
      if (i === 0) g.moveTo(x, y);
      else g.lineTo(x, y);
    });
    g.stroke();
  }

  function drawStatic() {
    const dpr = window.devicePixelRatio || 1;
    layer.width = Math.round(width * dpr);
    layer.height = Math.round(height * dpr);
    const g = lctx;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, width, height);
    g.font = "11px var(--font-ui, system-ui)";
    g.textBaseline = "middle";
    CHANNELS.forEach((name, j) => {
      const top = j * (STRIP_H + GAP);
      g.fillStyle = "rgb(255 255 255 / 2.5%)";
      g.fillRect(LABEL_W, top, plotW(), STRIP_H);
      const [lo, hi] = range(j);
      if (lo < 0 && hi > 0) {
        g.strokeStyle = C.hair;
        g.lineWidth = 1;
        g.beginPath();
        g.moveTo(LABEL_W, yOf(j, 0));
        g.lineTo(LABEL_W + plotW(), yOf(j, 0));
        g.stroke();
      }
      g.fillStyle = C.text;
      g.textAlign = "left";
      g.fillText(name, 4, top + STRIP_H / 2 - 5);
      g.fillStyle = C.muted;
      g.fillText(`${DSL_KEYS[j]} · ${lo}..${hi}${UNITS[j]}`, 4, top + STRIP_H / 2 + 8);
    });
    if (!series || !duration) return;
    const { frames, plan, motion, fps } = series;
    if (frames) {
      const ts = frames.map((_, i) => i / FPS);
      g.strokeStyle = "rgb(255 255 255 / 16%)";
      g.lineWidth = 1;
      g.setLineDash([]);
      CHANNELS.forEach((_, j) =>
        path(
          g,
          j,
          ts,
          frames.map((r) => r[j]),
        ),
      );
    }
    if (plan) {
      const ts = plan.keys.map((k) => k.t);
      g.strokeStyle = C.plan;
      g.fillStyle = C.plan;
      g.lineWidth = 1.2;
      g.setLineDash([4, 3]);
      CHANNELS.forEach((c, j) =>
        path(
          g,
          j,
          ts,
          plan.keys.map((k) => k[c]),
        ),
      );
      g.setLineDash([]);
      CHANNELS.forEach((c, j) =>
        plan.keys.forEach((k) => {
          g.beginPath();
          g.arc(xOf(k.t), yOf(j, k[c]), 1.8, 0, 2 * Math.PI);
          g.fill();
        }),
      );
    }
    const ts = motion.map((_, i) => i / fps);
    g.strokeStyle = C.text;
    g.lineWidth = 1.5;
    motion[0]?.forEach((_, j) =>
      path(
        g,
        j,
        ts,
        motion.map((r) => r[j]),
      ),
    );
    g.fillStyle = C.muted;
    g.textAlign = "center";
    const axisY = CHANNELS.length * (STRIP_H + GAP) + 8;
    const step = TICK_STEPS.find((s) => duration / s <= 12) ?? duration / 12;
    for (let t = 0; t <= duration + 1e-9; t += step) g.fillText(`${+t.toFixed(1)}s`, xOf(t), axisY);
  }

  function draw() {
    const dpr = window.devicePixelRatio || 1;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (!layer.width) return;
    ctx.drawImage(layer, 0, 0);
    if (!series || !duration) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const x = xOf(playhead);
    ctx.strokeStyle = C.accent;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, CHANNELS.length * (STRIP_H + GAP) - GAP);
    ctx.stroke();
    const i = Math.min(series.motion.length - 1, Math.round(playhead * series.fps));
    const key = series.plan?.keys.reduce((a, b) => (Math.abs(b.t - playhead) < Math.abs(a.t - playhead) ? b : a));
    ctx.font = "11px var(--font-mono, ui-monospace, monospace)";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    ctx.fillStyle = C.text;
    CHANNELS.forEach((c, j) => {
      const v = j < ENERGY ? series?.motion[i]?.[j] : key?.energy;
      if (typeof v !== "number") return;
      ctx.fillText(v.toFixed(j === 5 ? 0 : 2), width - 4, j * (STRIP_H + GAP) + STRIP_H / 2);
    });
  }

  function resize() {
    if (!container.clientWidth) return; // detached or hidden: keep the last layout
    width = container.clientWidth;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    drawStatic();
    draw();
  }
  const observer = new ResizeObserver(resize);
  observer.observe(container);

  /** @param {PointerEvent} ev */
  function scrub(ev) {
    if (!duration) return;
    const r = canvas.getBoundingClientRect();
    const u = (ev.clientX - r.left - LABEL_W) / plotW();
    onSeek(Math.min(1, Math.max(0, u)) * duration);
  }
  let dragging = false;
  canvas.addEventListener("pointerdown", (ev) => {
    dragging = true;
    canvas.setPointerCapture(ev.pointerId);
    scrub(ev);
  });
  canvas.addEventListener("pointermove", (ev) => dragging && scrub(ev));
  canvas.addEventListener("pointerup", () => (dragging = false));
  canvas.addEventListener("pointercancel", () => (dragging = false));
  resize();

  return {
    /** @param {Series} next */
    set(next) {
      series = next;
      duration = next.duration;
      playhead = 0;
      drawStatic();
      draw();
    },
    /** @param {number} t */
    setTime(t) {
      if (Math.abs(t - playhead) < 1e-4) return;
      playhead = t;
      draw();
    },
    destroy() {
      observer.disconnect();
      canvas.remove();
    },
  };
}
