// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// The observation window, full width across the top of the policy page.
//
// Two facts, one picture. The strip is the frames the model was handed, each
// drawn at the resolution it was handed them at. The ruler above it is every
// keyframe of the episode so far, with the sixteen the window picked marked and
// tied to the frame they became -- which is what makes uniform sampling visible
// as sampling rather than as "the last sixteen frames".

// The gap introspect.py leaves between cells, in the composed image's pixels.
const CELL_GAP = 3;
const RULER_H = 26;
const MAX_STRIP_H = 200;

const COLORS = {
  tick: "rgba(255, 255, 255, 0.16)",
  picked: "#ffd700",
  link: "rgba(255, 215, 0, 0.35)",
  live: "#00ff88",
};

/**
 * Where each cell sits in the composed strip, in the image's own pixels.
 * @param {number[][]} sizes @param {number} naturalW
 * @returns {{ x: number, w: number }[] | null} null when the sizes do not
 *   describe this image -- they arrive on a different message than the strip,
 *   so they can be a plan out of step, and a ruler drawn from stale geometry
 *   points at the wrong frames.
 */
function cellLayout(sizes, naturalW) {
  if (!sizes.length) return null;
  const cells = [];
  let x = 0;
  for (const [w] of sizes) {
    cells.push({ x, w });
    x += w + CELL_GAP;
  }
  return x - CELL_GAP === naturalW ? cells : null;
}

export function createObsBar(parent) {
  const root = document.createElement("div");
  root.className = "policy-obsbar";
  root.innerHTML = `
    <div class="obs-head">
      <span class="obs-title">Observation window</span>
      <span class="obs-note">waiting for a plan</span>
      <button class="obs-scale" type="button" title="show the frames at the exact size the model saw them">1:1</button>
    </div>
    <div class="obs-scroll">
      <canvas class="obs-ruler"></canvas>
      <div class="obs-empty">the frames this plan was made from</div>
    </div>`;
  parent.appendChild(root);

  const note = /** @type {HTMLElement} */ (root.querySelector(".obs-note"));
  const scaleBtn = /** @type {HTMLButtonElement} */ (root.querySelector(".obs-scale"));
  const scroll = /** @type {HTMLElement} */ (root.querySelector(".obs-scroll"));
  const canvas = /** @type {HTMLCanvasElement} */ (root.querySelector(".obs-ruler"));
  const ctx = canvas.getContext("2d");
  const strip = new Image();
  strip.className = "obs-strip";
  strip.alt = "the frames this plan was made from, each at the size the model saw it";

  let oneToOne = false;
  /** @type {any} the plan the strip on screen was composed from */
  let shown = null;
  /** @type {any} the newest plan, whatever the strip is showing */
  let latest = null;
  /** @type {Map<number, any>} recent plans by seq, to pair a strip with the
   *  window it was actually made from rather than with whatever arrived last. */
  const plans = new Map();
  /** The strip's plan, until its status snapshot catches up. The node composes
   *  the strip the moment a plan lands and reports it on the next status tick,
   *  so the image always arrives first and the ruler has to wait for it. */
  let wantSeq = null;

  const dpr = () => Math.min(window.devicePixelRatio || 1, 2);

  /** Rendered strip width in CSS pixels: the column, or the image's own. */
  function renderWidth() {
    const avail = scroll.clientWidth || root.clientWidth;
    if (!strip.naturalWidth) return avail;
    if (oneToOne) return strip.naturalWidth;
    return Math.min(avail, strip.naturalWidth);
  }

  function layout() {
    const w = renderWidth();
    if (strip.naturalWidth) {
      // Fitted, the bar must not grow tall enough to push the scene off the
      // page. At 1:1 the exact pixels are the whole point, so it may.
      const h = (strip.naturalHeight * w) / strip.naturalWidth;
      strip.style.width = !oneToOne && h > MAX_STRIP_H ? `${(w * MAX_STRIP_H) / h}px` : `${w}px`;
    }
    drawRuler();
  }

  function drawRuler() {
    if (!ctx) return;
    const w = Math.max(1, parseFloat(strip.style.width) || renderWidth());
    const d = dpr();
    canvas.width = Math.round(w * d);
    canvas.height = Math.round(RULER_H * d);
    canvas.style.width = `${w}px`;
    canvas.style.height = `${RULER_H}px`;
    ctx.setTransform(d, 0, 0, d, 0, 0);
    ctx.clearRect(0, 0, w, RULER_H);
    if (!shown) return;

    const picked = shown.history_indices || [];
    // Indices are ordinals into the keyframe list, and the live frame sits one
    // past the last keyframe it has not earned yet -- so the track is as long
    // as the furthest index, not as the keyframe count.
    const slots = Math.max(shown.keyframes || 0, (picked.at(-1) ?? 0) + 1);
    if (slots <= 0) return;
    const cells = cellLayout(shown.history_sizes || [], strip.naturalWidth);
    const scale = strip.naturalWidth ? w / strip.naturalWidth : 1;
    const at = (k) => ((k + 0.5) / slots) * w;
    const centre = (i) =>
      cells && cells[i] ? (cells[i].x + cells[i].w / 2) * scale : ((i + 0.5) / picked.length) * w;

    const top = 4;
    const mark = 9;
    ctx.strokeStyle = COLORS.tick;
    ctx.lineWidth = Math.max(1, w / slots > 6 ? 2 : 1);
    ctx.beginPath();
    for (let k = 0; k < slots; k++) {
      const x = at(k);
      ctx.moveTo(x, top + 2);
      ctx.lineTo(x, top + mark - 2);
    }
    ctx.stroke();

    // Each pick, tied to the frame it became. The lines are what say "these
    // sixteen, spread over all of those" rather than "sixteen frames".
    picked.forEach((k, i) => {
      const isLive = k >= (shown.keyframes || 0);
      ctx.strokeStyle = isLive ? COLORS.live : COLORS.picked;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(at(k), top);
      ctx.lineTo(at(k), top + mark);
      ctx.stroke();

      ctx.strokeStyle = isLive ? COLORS.live : COLORS.link;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(at(k), top + mark);
      ctx.lineTo(centre(i), RULER_H - 1);
      ctx.stroke();
    });
  }

  function setNote() {
    const p = latest;
    if (!p) {
      note.textContent = "waiting for a plan";
      return;
    }
    const sizes = p.history_sizes || [];
    const ages = p.history_ages_s || [];
    const slots = Math.max(p.keyframes || 0, (p.history_indices?.at(-1) ?? 0) + 1);
    // The age spread is the tell: bunched at the right means recency-only,
    // evenly spread means the whole episode is in view.
    const parts = [`${p.history_indices.length} of ${slots} observations, oldest first`];
    if (sizes.length) parts.push(`${sizes[0].join("×")} … ${sizes.at(-1).join("×")}`);
    if (ages.length) parts.push(`age ${ages[0].toFixed(1)}s … ${ages.at(-1).toFixed(1)}s`);
    note.textContent = parts.join(" · ");
  }

  /** @param {any} s the parsed /nav_policy/status payload */
  function setStatus(s) {
    latest = s.plan || null;
    if (latest) {
      plans.set(latest.seq, latest);
      while (plans.size > 24) plans.delete(plans.keys().next().value);
      if (latest.seq === wantSeq && shown !== latest) {
        shown = latest;
        drawRuler();
      }
    }
    setNote();
  }

  /** @param {string} b64 @param {string|number|undefined} seq which plan the
   *  node composed this strip from, carried in the image header. */
  function setStrip(b64, seq) {
    wantSeq = Number(seq);
    // Something to draw now; the exact window follows a status tick later.
    shown = plans.get(wantSeq) ?? shown ?? latest;
    const pinned = scroll.scrollLeft >= scroll.scrollWidth - scroll.clientWidth - 4;
    strip.src = `data:image/jpeg;base64,${b64}`;
    if (strip.parentElement !== scroll) {
      root.querySelector(".obs-empty")?.remove();
      scroll.appendChild(strip);
    }
    if (pinned) requestAnimationFrame(() => { scroll.scrollLeft = scroll.scrollWidth; });
  }

  function clear() {
    shown = latest = null;
    wantSeq = null;
    plans.clear();
    strip.removeAttribute("src");
    strip.remove();
    if (!root.querySelector(".obs-empty")) {
      scroll.appendChild(Object.assign(document.createElement("div"),
        { className: "obs-empty", textContent: "no episode running" }));
    }
    note.textContent = "no episode running";
    drawRuler();
  }

  strip.addEventListener("load", layout);
  scaleBtn.addEventListener("click", () => {
    oneToOne = !oneToOne;
    scaleBtn.classList.toggle("on", oneToOne);
    scaleBtn.textContent = oneToOne ? "fit" : "1:1";
    layout();
  });
  const onResize = () => layout();
  window.addEventListener("resize", onResize);

  return {
    setStatus,
    setStrip,
    clear,
    destroy() {
      window.removeEventListener("resize", onResize);
      root.remove();
    },
  };
}
