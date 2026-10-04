// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Expression Studio — type a feeling, watch MARS perform it, send it to the robot.
//
// A prompt becomes a recipe (from the robot's own generate path, the 5090
// planner server, or Gemini in the browser — planner.js), the recipe becomes a
// lively 25 Hz clip through the JS port of the expressive core (pipeline.js),
// and the clip plays on the full-robot 3D model (viz.js) through the same basis
// the robot uses. Nothing here needs a robot; Apply publishes the clip on
// /brain/express/play for the robot's (or the sim's) animator to perform.

import {
  FPS,
  NEUTRAL,
  breathing,
  checkRecipe,
  clipActuators,
  clipJson,
  expandRecipe,
  limitFrames,
  limitSpeed,
  parseClip,
  recipeToClip,
  synthesize,
  toPlan,
} from "./pipeline.js";
import { Player, sampleTake, takeDuration } from "./player.js";
import { createTimeline } from "./plots.js";
import { DEFAULT_PRESET, PRESETS, nearestPreset } from "./presets.js";
import { createRobotPlanner, planWithGemini, planWithServer } from "./planner.js";
import { loadGeminiConfig, saveGeminiConfig } from "./judge.js";
import { createJudgeCard } from "./judgeCard.js";
import { createStage, THUMB_VIEW, VIEWS } from "./viz.js";
import { ros } from "../rosClient.js";

/** @typedef {import("./pipeline.js").Basis} Basis */
/** @typedef {import("./pipeline.js").Clip} Clip */
/** @typedef {import("./pipeline.js").Plan} Plan */
/** @typedef {import("./judge.js").KeyFrame} KeyFrame */
/**
 * A clip ready to play, with the stages that made it (for the plots).
 * @typedef {import("./player.js").Take & { frames: number[][] | null, plan: Plan | null,
 *            source: string, detail: string, seed: number, placeholder?: boolean }} StudioTake
 */

const PLAY_TOPIC = "/brain/express/play";
const STOP_TOPIC = "/brain/express/stop";
const STATE_TOPIC = "/brain/express/state";
const STYLESHEET = "/css/expression.css";
// The robot's own basis.json (served from the brain_client package), so the
// preview maps motion exactly as the robot will.
const BASIS_URL = "/expression/basis.json";
const PREFS_KEY = "innate.expression.studio";
const DEFAULT_SERVER = "http://innate52.local:8000";
const PLACEHOLDER_AFTER_MS = 350;
const SOURCE_TIMEOUT_MS = 12_000;
const KEY_FRAME_AT = [0.12, 0.37, 0.62, 0.87];
const TRIES = [
  "a curious puppy",
  "stepping on a lego",
  "a cat spotting a cucumber",
  "proud, you finally solved it",
  "sneezing",
  "a sleepy toddler fighting to stay awake",
];

const ICON = {
  play: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.5v13l10.5-6.5z"/></svg>',
  pause:
    '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6.5" y="5.5" width="4" height="13" rx="1"/><rect x="13.5" y="5.5" width="4" height="13" rx="1"/></svg>',
  stop: '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6.5" y="6.5" width="11" height="11" rx="1.5"/></svg>',
  loop: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M17 2.5l3 3-3 3"/><path d="M4 11.5v-1a5 5 0 0 1 5-5h11"/><path d="M7 21.5l-3-3 3-3"/><path d="M20 12.5v1a5 5 0 0 1-5 5H4"/></svg>',
  rec: '<svg viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="6"/></svg>',
};

const PAGE_HTML = `
  <div class="page-head">
    <h1 class="page-title">Expression Studio</h1>
    <span class="exs-sub">type a feeling · watch MARS perform it · send it to the robot</span>
  </div>
  <div class="exs-body">
    <div class="exs-ask">
      <div class="exs-ask-field">
        <input type="text" data-el="prompt" autocomplete="off" spellcheck="false"
          placeholder="a curious puppy hearing a strange noise…" aria-label="Describe a feeling, a character or a moment">
        <span class="exs-ask-hint">Enter ↵</span>
      </div>
      <select data-el="source" title="Where the recipe comes from" aria-label="Recipe source">
        <option value="auto">auto</option>
        <option value="llm">browser LLM</option>
        <option value="robot">robot</option>
        <option value="server">5090 server</option>
      </select>
      <button class="exs-primary" data-el="go">Perform</button>
    </div>
    <div class="exs-status" data-el="status"></div>
    <div class="exs-tries" data-el="tries"><span class="exs-sub">try</span></div>

    <div class="exs-main">
      <div class="exs-card exs-stage">
        <div class="exs-canvas" data-el="canvas"></div>
        <div class="exs-loading" data-el="loading">loading MARS…</div>
        <div class="exs-overlay exs-title">
          <b data-el="clipName"></b><span data-el="clipIdea"></span><em class="exs-source" data-el="clipSource" hidden></em>
        </div>
        <div class="exs-overlay exs-views" data-el="views"></div>
        <div class="exs-overlay exs-queue" data-el="queue"></div>
        <div class="exs-overlay exs-transport">
          <button data-el="playBtn" title="Play / pause (Space)" aria-label="Play or pause"></button>
          <button data-el="stopBtn" title="Stop — ease back into idle breathing" aria-label="Stop">${ICON.stop}</button>
          <button data-el="loopBtn" title="Loop" aria-label="Loop">${ICON.loop}</button>
          <input type="range" data-el="scrub" min="0" max="1" step="0.001" value="0" aria-label="Scrub">
          <span class="exs-time" data-el="time">0.0 / 0.0 s</span>
          <button data-el="recBtn" title="Record this clip from the start to a .webm" aria-label="Record">${ICON.rec}</button>
        </div>
      </div>
      <div class="exs-side">
        <div class="exs-card">
          <h2>Recipe <span class="spacer"></span><span class="exs-sub" data-el="seedInfo"></span></h2>
          <textarea class="exs-recipe" data-el="recipe" spellcheck="false" rows="4" aria-label="Recipe"></textarea>
          <div class="exs-check" data-el="check"></div>
          <div class="exs-row">
            <button class="exs-primary" data-el="expand" title="Expand the edited recipe and play it (⌘/Ctrl+Enter)">Re-expand</button>
            <button data-el="vary" title="Same recipe, a new liveliness seed">Vary</button>
            <span class="spacer"></span>
            <button class="exs-mini" data-el="exportClip" title="Download this clip as JSON">Export</button>
            <button class="exs-mini" data-el="importClip" title="Load a clip JSON (plan or actuator space)">Import</button>
            <input type="file" data-el="importFile" accept="application/json,.json" hidden>
          </div>
          <div class="exs-dsl"><code>go D k=v</code> ease · <code>hold D E=v</code> stay · <code>osc D ch amp per</code> oscillate<br>
            <code>a</code> approach <code>x</code> expand <code>z</code> rise <code>p</code> attend <code>k</code> askew
            <code>b</code> orient° <code>d</code> advance m <code>g</code> grip <code>E</code> energy</div>
        </div>
        <div class="exs-card">
          <h2>Robot <span class="exs-pill" data-el="conn">offline</span></h2>
          <div class="exs-row">
            <button class="exs-primary" data-el="apply" title="Play this clip on the robot (or the sim)">Apply to robot</button>
            <button class="exs-danger" data-el="robotStop" title="Stop the robot's expression">Stop</button>
            <label title="Also send every clip played here to the robot"><input type="checkbox" data-el="follow"
              aria-label="follow: also send every clip played here to the robot"> follow</label>
          </div>
          <dl class="exs-state" data-el="state"></dl>
          <div class="exs-meter"><i data-el="meter"></i></div>
        </div>
      </div>
    </div>

    <div class="exs-card">
      <h2>Timeline <span class="spacer"></span>
        <span class="exs-legend"><span class="raw"><i></i>recipe</span><span class="plan"><i></i>plan keys</span>
          <span class="motion"><i></i>motion</span><span class="now"><i></i>now</span></span></h2>
      <div class="exs-timeline" data-el="timeline"></div>
    </div>

    <div class="exs-card">
      <h2>Presets <span class="exs-sub">click to play · + to queue after the current clip</span></h2>
      <div class="exs-gallery" data-el="gallery"></div>
    </div>

    <div class="exs-bottom">
      <div class="exs-card">
        <h2>Blind judge <span class="spacer"></span><span class="exs-sub" data-el="judgeStatus"></span></h2>
        <div class="exs-sub">key frames from the person's eyes · the judge never sees the prompt</div>
        <div class="exs-frames" data-el="frames"></div>
        <div class="exs-row">
          <button class="exs-primary" data-el="evalBtn" title="Ask the judges, blind, what this motion reads as">Evaluate</button>
          <label title="What you are aiming for — highlighted in the result, never sent to the blind judges">target
            <select data-el="target"></select></label>
          <span class="spacer"></span>
          <button data-el="pinBtn" title="Hold this clip's key frames as comparison side A">Pin as A</button>
          <span class="exs-pin" data-el="pinInfo" hidden><img data-el="pinImg" alt=""><span class="exs-sub" data-el="pinName"></span></span>
          <button data-el="abBtn" title="Which of pinned (A) and current (B) reads more as the target">A/B vs pinned</button>
        </div>
        <div data-el="result" hidden>
          <div class="exs-bars" data-el="bars"></div>
          <ul class="exs-quotes" data-el="quotes"></ul>
        </div>
        <div class="exs-log" data-el="log"></div>
      </div>
      <div class="exs-card">
        <h2>Settings</h2>
        <div class="exs-settings">
          <label>Gemini API key — browser LLM + judge, kept in this browser
            <input type="password" data-el="apiKey" autocomplete="off" spellcheck="false"></label>
          <label>planner model <input type="text" data-el="plannerModel" spellcheck="false"></label>
          <label>judge model <input type="text" data-el="judgeModel" spellcheck="false"></label>
          <label>judges per evaluation <input type="number" data-el="judges" min="1" max="5" step="1"></label>
          <label>5090 planner server <input type="text" data-el="serverUrl" spellcheck="false"></label>
        </div>
        <p class="exs-sub">auto tries the browser LLM (when a key is set), then the robot, then the 5090 server, and
          plays the nearest preset while they think.</p>
      </div>
    </div>
  </div>`;

/** @returns {Promise<void>} */
function loadStylesheet() {
  if (document.querySelector(`link[href="${STYLESHEET}"]`)) return Promise.resolve();
  return new Promise((resolve) => {
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = STYLESHEET;
    link.onload = link.onerror = () => resolve();
    document.head.appendChild(link);
  });
}

/** @type {Promise<Basis> | null} */
let basisRequest = null;

/** The basis, fetched once per page load; a failure is retried on the next mount. @returns {Promise<Basis>} */
function loadBasis() {
  basisRequest ??= fetch(BASIS_URL).then(async (res) => {
    if (!res.ok) throw new Error(`${BASIS_URL}: HTTP ${res.status} ${(await res.text()).slice(0, 120)}`);
    return /** @type {Promise<Basis>} */ (res.json());
  });
  basisRequest.catch(() => (basisRequest = null));
  return basisRequest;
}

/** The page in its no-basis state: nothing can be previewed without it. @param {HTMLElement} stage @param {unknown} err */
function mountUnavailable(stage, err) {
  const root = document.createElement("div");
  root.className = "exs";
  root.innerHTML = `
    <div class="page-head"><h1 class="page-title">Expression Studio</h1></div>
    <div class="exs-body"><div class="exs-card exs-unavailable">
      <h2>Can't load MARS's expressive basis</h2>
      <p>The studio maps every motion onto the robot through <code>${BASIS_URL}</code> (brain_client's
        basis.json), and that request failed:</p>
      <pre data-el="why"></pre>
      <p class="exs-sub">Check that brain_client is built on this robot, then reload.</p>
    </div></div>`;
  /** @type {HTMLElement} */ (root.querySelector('[data-el="why"]')).textContent = message(err);
  stage.appendChild(root);
  return { destroy: () => root.remove() };
}

/** @returns {{ source: string, serverUrl: string, follow: boolean, view: string, loop: boolean }} */
function loadPrefs() {
  const defaults = { source: "auto", serverUrl: DEFAULT_SERVER, follow: false, view: "quarter", loop: false };
  try {
    return { ...defaults, ...JSON.parse(localStorage.getItem(PREFS_KEY) || "{}") };
  } catch {
    return defaults;
  }
}

/** The frame that departs most from NEUTRAL — a clip's most telling still. @param {Clip} clip */
function peakFrame(clip) {
  const scale = [1, 1, 1, 1, 1, 60, 0.25, 1];
  let best = 0;
  let bestScore = -1;
  clip.frames.forEach((row, i) => {
    const score = row.reduce((sum, v, j) => sum + ((v - NEUTRAL[j]) / scale[j]) ** 2, 0);
    if (score > bestScore) {
      best = i;
      bestScore = score;
    }
  });
  return best;
}

/** @param {unknown} err */
const message = (err) => (err instanceof Error ? err.message : String(err));

/** @param {HTMLElement} stage */
export async function mount(stage) {
  /** @type {Basis} */
  let basis;
  try {
    [basis] = await Promise.all([loadBasis(), loadStylesheet()]);
  } catch (err) {
    return mountUnavailable(stage, err);
  }

  const root = document.createElement("div");
  root.className = "exs";
  root.innerHTML = PAGE_HTML;
  stage.appendChild(root);

  /** @param {string} name */
  const el = (name) => /** @type {HTMLElement} */ (root.querySelector(`[data-el="${name}"]`));
  /** @param {string} name */
  const input = (name) => /** @type {HTMLInputElement} */ (el(name));
  /** @param {string} name */
  const button = (name) => /** @type {HTMLButtonElement} */ (el(name));

  let destroyed = false;
  const prefs = loadPrefs();
  const savePrefs = () => {
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
    } catch {
      // storage blocked: the studio works, it just forgets
    }
  };
  const gemini = loadGeminiConfig();
  /** @type {Awaited<ReturnType<typeof createStage>> | null} */
  let stageApi = null;

  // ---- takes & playback -----------------------------------------------------
  /**
   * @param {Clip} clip
   * @param {{ source: string, detail?: string, seed?: number, frames?: number[][] | null, plan?: Plan | null }} info
   * @returns {StudioTake}
   */
  function makeTake(clip, { source, detail = "", seed = 0, frames = null, plan = null }) {
    if (!frames && clip.space === "plan" && clip.recipe && !checkRecipe(clip.recipe)) {
      frames = expandRecipe(clip.recipe);
      plan = toPlan(frames);
    }
    const poses = limitFrames(basis, clipActuators(basis, clip), 1 / clip.fps);
    return { clip, poses, frames, plan, source, detail, seed };
  }

  /** @param {string} recipe @param {{ name?: string, prompt?: string, idea?: string, seed?: number, source: string, detail?: string }} meta */
  function takeFromRecipe(recipe, { source, detail = "", ...meta }) {
    const { clip, frames, plan } = recipeToClip(recipe, meta);
    return makeTake(clip, { source, detail, seed: meta.seed ?? 0, frames, plan });
  }

  const presetTakes = new Map(
    PRESETS.map((p) => [
      p.name,
      takeFromRecipe(p.recipe, { name: p.name, prompt: p.prompt, idea: p.idea, source: "preset" }),
    ]),
  );
  /** @param {string} name */
  const presetTake = (name) => /** @type {StudioTake} */ (presetTakes.get(name));

  // Following mirrors each take to the robot as it starts here (queued ones too),
  // but not the placeholder that only fills the wait for a planner.
  /** @type {Player<StudioTake>} */
  const player = new Player(
    (t) => synthesize(basis, breathing(t)),
    (take) => {
      showTake(take);
      if (take && !take.placeholder && input("follow").checked) applyToRobot(take.clip);
    },
  );
  player.loop = prefs.loop;

  /** Play a take now, or after the queue. @param {StudioTake} take @param {boolean} [queue] */
  function perform(take, queue = false) {
    if (queue) player.enqueue(take);
    else player.play(take);
    renderNow();
  }

  const timeline = createTimeline(el("timeline"), {
    onSeek(t) {
      player.seek(t);
      renderNow();
    },
  });

  let framesTimer = 0;
  /** @param {StudioTake | null} take */
  function showTake(take) {
    if (!take) return;
    const clip = take.clip;
    el("clipName").textContent = clip.prompt || clip.name;
    el("clipIdea").textContent = clip.idea;
    el("clipSource").hidden = false;
    el("clipSource").textContent = take.detail ? `${take.source} · ${take.detail}` : take.source;
    /** @type {HTMLTextAreaElement} */ (el("recipe")).value = clip.recipe;
    el("seedInfo").textContent = clip.space === "actuator" ? "recorded actuator clip" : `seed ${take.seed}`;
    validateRecipe();
    timeline.set({
      frames: take.frames,
      plan: take.plan,
      motion: clip.space === "plan" ? clip.frames : [],
      fps: clip.fps,
      duration: takeDuration(take),
    });
    root.querySelectorAll(".exs-preset").forEach((card) => {
      card.classList.toggle(
        "active",
        /** @type {HTMLElement} */ (card).dataset.name === clip.name && take.source === "preset",
      );
    });
    // What the blind judge would see, refreshed once the take settles in.
    clearTimeout(framesTimer);
    framesTimer = setTimeout(() => destroyed || judgeCard.showFrames(), 400);
  }

  // ---- the render loop and transport -------------------------------------------
  let shownPlaying = /** @type {boolean | null} */ (null);
  let scrubbing = false;
  function syncTransport() {
    const take = player.take;
    const duration = take ? takeDuration(take) : 0;
    if (shownPlaying !== player.playing) {
      shownPlaying = player.playing;
      el("playBtn").innerHTML = player.playing ? ICON.pause : ICON.play;
    }
    if (!scrubbing) input("scrub").value = String(duration ? Math.min(1, player.t / duration) : 0);
    const t = Math.min(player.t, duration);
    el("time").textContent = `${t.toFixed(1)} / ${duration.toFixed(1)} s`;
    const names = player.queue.map((t) => t.clip.name);
    const queueText = names.join("|");
    if (el("queue").dataset.names !== queueText) {
      el("queue").dataset.names = queueText;
      el("queue").replaceChildren(
        ...names.map((n, i) => {
          const chip = document.createElement("span");
          chip.textContent = `${i === 0 ? "next" : "then"} · ${n}`;
          return chip;
        }),
      );
    }
  }

  // The robot's animator rate-limits every pose; showing the same keeps the preview honest.
  let shown = player.pose;
  /** @param {number} dt */
  function tick(dt) {
    shown = limitSpeed(basis, shown, player.advance(dt), dt);
    stageApi?.setPose(shown);
    timeline.setTime(Math.min(player.t, player.duration));
    syncTransport();
  }

  /** Show the player's pose now — seeks and clicks must not wait for (or, in a hidden tab, lack) a frame. */
  function renderNow() {
    shown = player.pose;
    stageApi?.setPose(shown);
    stageApi?.render();
    timeline.setTime(Math.min(player.t, player.duration));
    syncTransport();
  }

  el("playBtn").addEventListener("click", () => {
    player.toggle();
    renderNow();
  });
  el("stopBtn").addEventListener("click", () => {
    cancelAsk();
    player.stop();
    renderNow();
  });
  const loopBtn = el("loopBtn");
  loopBtn.classList.toggle("on", player.loop);
  loopBtn.addEventListener("click", () => {
    player.loop = prefs.loop = !player.loop;
    loopBtn.classList.toggle("on", player.loop);
    savePrefs();
  });
  input("scrub").addEventListener("input", () => {
    scrubbing = true;
    if (player.take) player.seek(+input("scrub").value * takeDuration(player.take));
    renderNow();
  });
  input("scrub").addEventListener("change", () => (scrubbing = false));

  /** @param {KeyboardEvent} ev */
  function onKey(ev) {
    if (ev.target instanceof Element && ev.target.closest("input, textarea, select, button, [contenteditable]")) return;
    if (ev.key === " ") {
      ev.preventDefault();
      player.toggle();
      renderNow();
    } else if (ev.key === "/") {
      ev.preventDefault();
      input("prompt").focus();
    }
  }
  document.addEventListener("keydown", onKey);

  // ---- views -----------------------------------------------------------------
  for (const view of VIEWS) {
    const b = document.createElement("button");
    b.textContent = view.label;
    b.dataset.view = view.key;
    b.addEventListener("click", () => {
      prefs.view = view.key;
      savePrefs();
      stageApi?.setView(view.key);
      markView(view.key);
    });
    el("views").appendChild(b);
  }
  /** @param {string} key */
  const markView = (key) =>
    el("views")
      .querySelectorAll("button")
      .forEach((b) => b.classList.toggle("active", b.dataset.view === key));
  markView(prefs.view);

  // ---- ask: prompt → recipe → clip ------------------------------------------------
  /** @type {AbortController | null} */
  let inflight = null;

  /** @param {string} text @param {"" | "busy" | "ok" | "err"} [kind] */
  function status(text, kind = "") {
    el("status").textContent = text;
    el("status").className = `exs-status ${kind}`;
  }

  /** @returns {{ name: string, run: (prompt: string, signal: AbortSignal) => Promise<import("./planner.js").Made> }[]} */
  function sourceChain() {
    const seed = Math.floor(Math.random() * 1e6);
    const llm = {
      name: "browser LLM",
      run: (/** @type {string} */ p, /** @type {AbortSignal} */ signal) =>
        planWithGemini(p, { apiKey: gemini.apiKey, model: gemini.plannerModel, seed, signal }),
    };
    const robot = {
      name: "robot",
      run: (/** @type {string} */ p, /** @type {AbortSignal} */ signal) => robotPlanner.generate(p, { signal }),
    };
    const server = {
      name: "5090 server",
      run: (/** @type {string} */ p, /** @type {AbortSignal} */ signal) =>
        planWithServer(p, { url: prefs.serverUrl, seed, signal }),
    };
    const choice = /** @type {HTMLSelectElement} */ (el("source")).value;
    if (choice === "llm") return [llm];
    if (choice === "robot") return [robot];
    if (choice === "server") return [server];
    return [...(gemini.apiKey ? [llm] : []), ...(ros.state === "connected" ? [robot] : []), server];
  }

  /** @param {AbortSignal} outer */
  function withTimeout(outer) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(new Error("timed out")), SOURCE_TIMEOUT_MS);
    const forward = () => ctrl.abort(outer.reason);
    outer.addEventListener("abort", forward, { once: true });
    return {
      signal: ctrl.signal,
      done: () => {
        clearTimeout(timer);
        outer.removeEventListener("abort", forward);
      },
    };
  }

  /** Drop a pending ask, so its answer can't start motion after a Stop. */
  function cancelAsk() {
    if (!inflight) return;
    inflight.abort();
    status("stopped");
  }

  /** @param {string} raw */
  async function ask(raw) {
    const prompt = raw.trim();
    if (!prompt) return;
    inflight?.abort();
    const ctrl = new AbortController();
    inflight = ctrl;
    const preset = nearestPreset(prompt);
    // The placeholder only plays when the planner is slow, so a fast answer
    // doesn't start one motion and crossfade away from it half-way.
    const placeholder = setTimeout(() => {
      const take = presetTake(preset.name);
      player.play({ ...take, detail: "nearest preset, while the planner thinks", placeholder: true });
      renderNow();
    }, PLACEHOLDER_AFTER_MS);
    const started = performance.now();
    /** @type {string[]} */
    const failures = [];
    try {
      for (const source of sourceChain()) {
        status(`${source.name} is planning “${prompt}”…`, "busy");
        const timed = withTimeout(ctrl.signal);
        try {
          const made = await source.run(prompt, timed.signal);
          if (ctrl.signal.aborted || destroyed) return;
          const take = makeTake({ ...made.clip, name: prompt, prompt }, { source: made.source, detail: made.detail });
          perform(take);
          status(
            `${made.source}${made.detail ? ` · ${made.detail}` : ""} · ${Math.round(performance.now() - started)} ms`,
            "ok",
          );
          return;
        } catch (err) {
          if (ctrl.signal.aborted || destroyed) return;
          failures.push(`${source.name}: ${message(err)}`);
        } finally {
          timed.done();
        }
      }
      const current = player.take;
      if (current?.placeholder && current.clip === presetTake(preset.name).clip) {
        // The stand-in becomes the answer: relabel it and let follow send it on.
        current.placeholder = false;
        current.detail = "no planner answered";
        showTake(current);
        if (input("follow").checked) applyToRobot(current.clip);
      } else {
        perform({ ...presetTake(preset.name), detail: "no planner answered" });
      }
      status(`no planner answered — playing the nearest preset “${preset.name}” · ${failures.join(" · ")}`, "err");
    } finally {
      clearTimeout(placeholder);
      if (inflight === ctrl) inflight = null;
    }
  }

  input("prompt").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") void ask(input("prompt").value);
  });
  el("go").addEventListener("click", () => void ask(input("prompt").value));
  const sourceSelect = /** @type {HTMLSelectElement} */ (el("source"));
  sourceSelect.value = prefs.source;
  sourceSelect.addEventListener("change", () => {
    prefs.source = sourceSelect.value;
    savePrefs();
  });
  for (const text of TRIES) {
    const b = document.createElement("button");
    b.textContent = text;
    b.addEventListener("click", () => {
      input("prompt").value = text;
      void ask(text);
    });
    el("tries").appendChild(b);
  }

  // ---- recipe editor ---------------------------------------------------------------
  const recipeBox = /** @type {HTMLTextAreaElement} */ (el("recipe"));
  function validateRecipe() {
    const recipe = recipeBox.value.trim();
    const error = recipe ? checkRecipe(recipe) : "";
    button("expand").disabled = button("vary").disabled = !recipe || !!error;
    if (!recipe) {
      el("check").textContent = "";
      recipeBox.classList.remove("bad");
      return false;
    }
    recipeBox.classList.toggle("bad", !!error);
    el("check").classList.toggle("bad", !!error);
    if (error) {
      el("check").textContent = `✕ ${error}`;
      return false;
    }
    const frames = expandRecipe(recipe).length;
    el("check").textContent = `✓ ${((frames - 1) / FPS).toFixed(2)} s · ${frames} frames`;
    return true;
  }
  recipeBox.addEventListener("input", validateRecipe);

  /** @param {number} seed */
  function reexpand(seed) {
    if (!validateRecipe()) return;
    const current = player.take;
    const prompt = current?.clip.prompt ?? "";
    const take = takeFromRecipe(recipeBox.value.trim(), {
      name: current?.clip.name || "edited recipe",
      prompt,
      idea: current?.clip.idea ?? "",
      seed,
      source: "edited",
      detail: "local liveliness",
    });
    perform(take);
  }
  el("expand").addEventListener("click", () => reexpand(player.take?.seed ?? 0));
  el("vary").addEventListener("click", () => reexpand((player.take?.seed ?? 0) + 1));
  recipeBox.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) {
      ev.preventDefault();
      reexpand(player.take?.seed ?? 0);
    }
  });

  /** @param {Blob} blob @param {string} filename */
  function download(blob, filename) {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 60_000);
    return a.href;
  }
  /** @param {string} name */
  const slug = (name) =>
    name
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 40) || "clip";

  el("exportClip").addEventListener("click", () => {
    const take = player.take;
    if (!take) return;
    download(new Blob([clipJson(take.clip)], { type: "application/json" }), `mars-${slug(take.clip.name)}.json`);
  });
  el("importClip").addEventListener("click", () => input("importFile").click());
  input("importFile").addEventListener("change", async () => {
    const file = input("importFile").files?.[0];
    input("importFile").value = "";
    if (!file) return;
    try {
      const clip = parseClip(await file.text());
      perform(makeTake(clip, { source: "file", detail: file.name }));
      log(`loaded ${file.name} (${clip.space}, ${clip.frames.length} frames)`, "ok");
    } catch (err) {
      log(`import failed: ${message(err)}`, "err");
    }
  });

  // ---- robot -------------------------------------------------------------------------
  const robotPlanner = createRobotPlanner(ros);
  const unadvertise = [
    ros.advertise(PLAY_TOPIC, "std_msgs/msg/String"),
    ros.advertise(STOP_TOPIC, "std_msgs/msg/Empty"),
  ];

  /** @param {Clip} clip */
  function applyToRobot(clip) {
    if (ros.state !== "connected" || !ros.publish(PLAY_TOPIC, { data: clipJson(clip) })) {
      log("robot not connected — the clip stays in the studio", "err");
      return;
    }
    log(`sent “${clip.prompt || clip.name}” to the robot (${clip.frames.length} frames)`, "ok");
  }
  el("apply").addEventListener("click", () => {
    if (player.take) applyToRobot(player.take.clip);
  });
  el("robotStop").addEventListener("click", () => {
    cancelAsk();
    if (!ros.publish(STOP_TOPIC, {})) log("robot not connected", "err");
  });
  input("follow").checked = prefs.follow;
  input("follow").addEventListener("change", () => {
    prefs.follow = input("follow").checked;
    savePrefs();
  });

  /** @param {Record<string, string>} rows */
  function showState(rows) {
    el("state").replaceChildren(
      ...Object.entries(rows).flatMap(([k, v]) => {
        const dt = document.createElement("dt");
        dt.textContent = k;
        const dd = document.createElement("dd");
        dd.textContent = v;
        dd.title = v;
        return [dt, dd];
      }),
    );
  }
  showState({ state: "no /brain/express/state yet" });
  let stateSeenAt = 0;
  const unsubState = ros.subscribe(
    STATE_TOPIC,
    (/** @type {{ data?: string }} */ msg) => {
      /** @type {any} */
      let s;
      try {
        s = JSON.parse(msg?.data ?? "");
      } catch {
        return;
      }
      stateSeenAt = performance.now();
      const masked = Array.isArray(s.masked) ? s.masked.length > 0 : !!s.masked;
      const flags = [masked && "masked by a skill", s.speaking && "speaking"].filter(Boolean).join(" · ");
      showState({
        doing: s.playing ? `playing “${s.name ?? "clip"}”` : "idle — breathing",
        time: s.playing ? `${Number(s.t).toFixed(1)} / ${Number(s.duration).toFixed(1)} s` : "—",
        source: s.source ?? "—",
        flags: flags || "—",
      });
      /** @type {HTMLElement} */ (el("meter")).style.width =
        s.playing && s.duration ? `${Math.min(100, (100 * s.t) / s.duration)}%` : "0";
    },
    undefined,
    "std_msgs/msg/String",
  );
  const staleTimer = setInterval(() => {
    if (stateSeenAt && performance.now() - stateSeenAt > 3000) {
      stateSeenAt = 0;
      showState({ state: "no state for 3 s — is the expressive driver running?" });
    }
  }, 1000);
  const unsubConn = ros.onStateChange((state) => {
    const pill = el("conn");
    pill.textContent = state === "connected" ? "connected" : state;
    pill.className = `exs-pill ${state === "connected" ? "on" : "off"}`;
    button("apply").disabled = state !== "connected";
    button("robotStop").disabled = state !== "connected";
  });

  // ---- presets gallery -------------------------------------------------------------
  for (const preset of PRESETS) {
    const card = document.createElement("button");
    card.className = "exs-preset";
    card.dataset.name = preset.name;
    card.title = `${preset.prompt}\n${preset.recipe}`;
    const ph = document.createElement("span");
    ph.className = "ph";
    const name = document.createElement("b");
    name.textContent = preset.name;
    const queue = document.createElement("span");
    queue.className = "q";
    queue.textContent = "+";
    queue.title = "queue after the current clip";
    card.append(ph, name, queue);
    card.addEventListener("click", (ev) => {
      const take = presetTake(preset.name);
      perform(take, ev.target === queue || ev.shiftKey);
    });
    el("gallery").appendChild(card);
  }

  function renderThumbnails() {
    if (!stageApi) return;
    const cards = /** @type {HTMLElement[]} */ ([...el("gallery").children]);
    const poses = PRESETS.map((p) => {
      const take = presetTake(p.name);
      return take.poses[peakFrame(take.clip)];
    });
    const shots = stageApi.stills(poses, player.pose, { view: THUMB_VIEW, size: 240, studio: true });
    shots.forEach((src, i) => {
      const img = document.createElement("img");
      img.alt = "";
      img.src = src;
      cards[i].querySelector(".ph")?.replaceWith(img);
    });
  }

  // ---- log + judge -------------------------------------------------------------------
  /** @param {string} text @param {"ok" | "err"} [cls] */
  function log(text, cls) {
    const line = document.createElement("div");
    if (cls) line.className = cls;
    line.textContent = `${new Date().toLocaleTimeString()}  ${text}`;
    el("log").appendChild(line);
    el("log").scrollTop = el("log").scrollHeight;
    return line;
  }

  /** The playing clip's key frames from the person's eyes, or null before the stage exists. @returns {KeyFrame[] | null} */
  function keyFrames() {
    const take = player.take;
    if (!stageApi || !take) return null;
    const duration = takeDuration(take);
    const times = KEY_FRAME_AT.map((f) => f * duration);
    const shots = stageApi.stills(
      times.map((t) => sampleTake(take, t)),
      player.pose,
      { view: "person", size: 384 },
    );
    return shots.map((dataUrl, i) => ({ dataUrl, t: times[i] }));
  }

  const judgeCard = createJudgeCard(el, {
    gemini,
    keyFrames,
    clipName: () => player.take?.clip.name ?? "",
    log,
    needKey: () => input("apiKey").focus(),
  });

  // ---- settings ------------------------------------------------------------------------
  input("apiKey").value = gemini.apiKey;
  input("plannerModel").value = gemini.plannerModel;
  input("judgeModel").value = gemini.judgeModel;
  input("judges").value = String(gemini.judges);
  for (const name of ["apiKey", "plannerModel", "judgeModel", "judges"]) {
    input(name).addEventListener("change", () => {
      gemini.apiKey = input("apiKey").value.trim();
      gemini.plannerModel = input("plannerModel").value.trim() || gemini.plannerModel;
      gemini.judgeModel = input("judgeModel").value.trim() || gemini.judgeModel;
      gemini.judges = Math.min(5, Math.max(1, +input("judges").value || 3));
      try {
        saveGeminiConfig(gemini);
      } catch {
        log("could not save settings (storage blocked)", "err");
      }
    });
  }
  input("serverUrl").value = prefs.serverUrl;
  input("serverUrl").addEventListener("change", () => {
    prefs.serverUrl = input("serverUrl").value.trim() || DEFAULT_SERVER;
    input("serverUrl").value = prefs.serverUrl;
    savePrefs();
  });

  // ---- recording ----------------------------------------------------------------------
  let recording = false;
  el("recBtn").addEventListener("click", async () => {
    const take = player.take;
    if (!stageApi || !take || recording) return;
    const clip = take.clip;
    /** @type {ReturnType<typeof stageApi.record>} */
    let recorder;
    try {
      recorder = stageApi.record({
        caption: clip.prompt || clip.name,
        sub: clip.recipe ? `MARS · ${clip.recipe}` : "MARS",
      });
    } catch (err) {
      log(`this browser can't record the canvas: ${message(err)}`, "err");
      return;
    }
    recording = true;
    el("recBtn").classList.add("rec");
    const looping = player.loop;
    player.loop = false;
    player.restart();
    renderNow();
    const deadline = performance.now() + (takeDuration(take) + 3) * 1000;
    await new Promise((resolve) => {
      const poll = setInterval(() => {
        const settled = !player.playing && !player.settle;
        if (destroyed || settled || performance.now() > deadline) {
          clearInterval(poll);
          setTimeout(resolve, 300);
        }
      }, 100);
    });
    const blob = await recorder.stop();
    player.loop = looping;
    recording = false;
    el("recBtn").classList.remove("rec");
    if (destroyed) return;
    const filename = `mars-${slug(clip.name)}.${blob.type.includes("mp4") ? "mp4" : "webm"}`;
    const href = download(blob, filename);
    const line = log(`recorded ${filename} (${(blob.size / 1e6).toFixed(1)} MB) — `, "ok");
    const again = document.createElement("a");
    again.href = href;
    again.download = filename;
    again.textContent = "download again";
    line.appendChild(again);
  });

  // ---- 3D stage ---------------------------------------------------------------------
  createStage(el("canvas"), { onFrame: tick, onOrbit: () => markView("") }).then(
    (api) => {
      if (destroyed) {
        api.destroy();
        return;
      }
      stageApi = api;
      el("loading").remove();
      api.setView(prefs.view, true);
      renderNow();
      renderThumbnails();
      judgeCard.showFrames();
    },
    (err) => {
      el("loading").textContent = `3D model unavailable (${message(err)}) — the timeline still works`;
    },
  );

  // A first-time visitor sees MARS alive at once (studio only — never sent to the robot unasked).
  player.play(presetTake(DEFAULT_PRESET));
  status("ready — type a feeling and press Enter (/ focuses the prompt)");
  input("prompt").focus({ preventScroll: true });

  return {
    destroy() {
      destroyed = true;
      inflight?.abort();
      clearTimeout(framesTimer);
      document.removeEventListener("keydown", onKey);
      clearInterval(staleTimer);
      unsubState();
      unsubConn();
      unadvertise.forEach((u) => u());
      robotPlanner.destroy();
      judgeCard.destroy();
      timeline.destroy();
      stageApi?.destroy();
      root.remove();
    },
  };
}
