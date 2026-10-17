// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// The studio's blind-judge card: key frames of the playing clip from the
// person's eyes, a blind panel's reading as bars, and A/B against a pinned clip.

import { JUDGE_LABELS, judgePair, judgePanel } from "./judge.js";

/** @typedef {import("./judge.js").KeyFrame} KeyFrame */
/** @typedef {import("./judge.js").GeminiConfig} GeminiConfig */
/** @typedef {import("./judge.js").PanelResult} PanelResult */

/** @param {unknown} err */
const message = (err) => (err instanceof Error ? err.message : String(err));

/**
 * @param {(name: string) => HTMLElement} el the page's data-el lookup
 * @param {{
 *   gemini: GeminiConfig,
 *   keyFrames: () => KeyFrame[] | null,
 *   clipName: () => string,
 *   log: (text: string, cls?: "ok" | "err") => void,
 *   needKey: () => void,
 * }} deps keyFrames renders the playing clip's frames (null when nothing can be rendered)
 */
export function createJudgeCard(el, { gemini, keyFrames, clipName, log, needKey }) {
  let alive = true;
  // Bumped whenever the shown frames change, so a late reading of an earlier clip isn't drawn under them.
  let shown = 0;
  const target = /** @type {HTMLSelectElement} */ (el("target"));
  for (const label of JUDGE_LABELS) {
    const opt = document.createElement("option");
    opt.value = opt.textContent = label;
    target.appendChild(opt);
  }

  /** Render the frames the judge would see, and show them. @returns {KeyFrame[] | null} */
  function showFrames() {
    const frames = keyFrames();
    if (!frames) return null;
    shown++;
    el("frames").replaceChildren(
      ...frames.map((f) => {
        const fig = document.createElement("figure");
        const img = document.createElement("img");
        img.src = f.dataUrl;
        img.alt = `t = ${f.t.toFixed(1)} s`;
        const cap = document.createElement("figcaption");
        cap.textContent = `${f.t.toFixed(1)} s`;
        fig.append(img, cap);
        return fig;
      }),
    );
    el("result").hidden = true;
    return frames;
  }

  /** Frames to judge, or null after telling the user what is missing. */
  function judgeable() {
    if (!gemini.apiKey) {
      log("set a Gemini API key in Settings first (kept only in this browser)", "err");
      needKey();
      return null;
    }
    const frames = showFrames();
    if (!frames) log("nothing to judge yet", "err");
    return frames;
  }

  /** @param {HTMLButtonElement} btn @param {string} busy @param {() => Promise<void>} work */
  async function busyWhile(btn, busy, work) {
    btn.disabled = true;
    el("judgeStatus").textContent = busy;
    try {
      await work();
    } catch (err) {
      log(`judge error: ${message(err)}`, "err");
    } finally {
      el("judgeStatus").textContent = "";
      btn.disabled = false;
    }
  }

  const evalBtn = /** @type {HTMLButtonElement} */ (el("evalBtn"));
  evalBtn.addEventListener("click", () => {
    const frames = judgeable();
    if (!frames) return;
    const goal = target.value;
    const judged = shown;
    const name = clipName();
    void busyWhile(evalBtn, `judging 0/${gemini.judges}…`, async () => {
      const panel = await judgePanel(frames, gemini, (done) => {
        el("judgeStatus").textContent = `judging ${done}/${gemini.judges}…`;
      });
      if (!alive) return;
      if (!panel.runs.length) {
        log(`all judges failed: ${panel.errors[0] ?? "unknown"}`, "err");
        return;
      }
      if (judged === shown) renderPanel(panel, goal);
      const top = JUDGE_LABELS.reduce((a, b) => (panel.mean[a] >= panel.mean[b] ? a : b));
      log(
        `“${name}” blind ×${panel.runs.length}: reads as ${top} ${panel.mean[top].toFixed(2)} · P(${goal}) ${panel.mean[goal].toFixed(2)}` +
          (panel.errors.length ? ` · ${panel.errors.length} judge(s) failed` : ""),
        top === goal ? "ok" : undefined,
      );
    });
  });

  /** @param {PanelResult} panel @param {string} goal */
  function renderPanel(panel, goal) {
    el("result").hidden = false;
    const sorted = [...JUDGE_LABELS].sort((a, b) => panel.mean[b] - panel.mean[a]);
    el("bars").replaceChildren(
      ...sorted
        .filter((label) => panel.mean[label] >= 0.02 || label === goal)
        .map((label) => {
          const row = document.createElement("div");
          row.className = `exs-bar${label === goal ? " target" : ""}`;
          if (panel.runs.length > 1) {
            const values = panel.runs.map((r) => r.probabilities[label]);
            row.title = `judges ranged ${Math.min(...values).toFixed(2)}–${Math.max(...values).toFixed(2)}`;
          }
          row.innerHTML =
            '<span class="lbl"></span><span class="track"><span class="fill"></span></span><span class="num"></span>';
          /** @type {HTMLElement} */ (row.querySelector(".lbl")).textContent = label;
          /** @type {HTMLElement} */ (row.querySelector(".fill")).style.width =
            `${(panel.mean[label] * 100).toFixed(1)}%`;
          /** @type {HTMLElement} */ (row.querySelector(".num")).textContent = panel.mean[label].toFixed(2);
          return row;
        }),
    );
    const cues = [...new Set(panel.runs.flatMap((r) => r.cues))].slice(0, 4);
    const impressions = [...new Set(panel.runs.map((r) => r.impression).filter(Boolean))].slice(0, 2);
    el("quotes").replaceChildren(
      ...[...impressions.map((t) => `“${t}”`), ...cues.map((t) => `cue: ${t}`)].map((t) => {
        const li = document.createElement("li");
        li.textContent = t;
        return li;
      }),
    );
  }

  /** @type {{ frames: KeyFrame[], name: string } | null} */
  let pinned = null;
  el("pinBtn").addEventListener("click", () => {
    const frames = showFrames();
    if (!frames) return;
    pinned = { frames, name: clipName() };
    el("pinInfo").hidden = false;
    /** @type {HTMLImageElement} */ (el("pinImg")).src = frames[1]?.dataUrl ?? "";
    el("pinName").textContent = pinned.name;
    log(`pinned “${pinned.name}” as A`);
  });

  const abBtn = /** @type {HTMLButtonElement} */ (el("abBtn"));
  abBtn.addEventListener("click", () => {
    const pins = pinned;
    if (!pins) {
      log("pin a clip as A first", "err");
      return;
    }
    const current = judgeable();
    if (!current) return;
    const goal = target.value;
    void busyWhile(abBtn, "comparing…", async () => {
      const verdicts = await Promise.all(
        Array.from({ length: gemini.judges }, () => judgePair(pins.frames, current, goal, gemini)),
      );
      if (!alive) return;
      const bWins = verdicts.filter((v) => v.winner === "B").length;
      const winner = bWins * 2 >= verdicts.length ? "current" : `pinned “${pins.name}”`;
      log(
        `A/B for “${goal}”: ${winner} wins ${Math.max(bWins, verdicts.length - bWins)}/${verdicts.length} — ${verdicts[0].reason}`,
        winner === "current" ? "ok" : undefined,
      );
    });
  });

  return {
    showFrames,
    destroy() {
      alive = false;
    },
  };
}
