// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// The studio's Speech mode: a reply the robot says, split and prompted per
// sentence exactly as the robot does (speech.js), every sentence's clip planned
// in order, then performed back to back on the 3D robot in time with the
// browser's voice — each clip starts when its sentence's audio does, and one that
// lands late starts when it lands, as the robot's driver cues them.

import { TTS_TOPIC } from "../constants.js";
import { planWithGemini, planWithServer, timeoutSignal } from "./planner.js";
import { takeDuration } from "./player.js";
import { nearestPreset } from "./presets.js";
import { replyBeats, speechSeconds } from "./speech.js";

/** @typedef {import("./speech.js").Beat} Beat */
/** @typedef {import("./planner.js").Made} Made */
/** @typedef {import("./player.js").Take & { title?: string }} SpeechTake */
/** @typedef {{ caption: (text: string, sub: string) => void }} Captioner */

const SOURCE_TIMEOUT_MS = 12_000;
// A cold voice engine takes ~3 s to start; one that never starts (no voices) must not stall the show.
const VOICE_START_TIMEOUT_MS = 4000;
const EFFORTS = ["low", "medium", "high"];
const SOURCES = [
  ["auto", "auto"],
  ["server", "5090 server"],
  ["robot", "robot"],
  ["llm", "browser LLM"],
  ["preset", "presets"],
];
export const TRIES = [
  {
    label: "a found sock",
    heard: "Hey MARS, what are you up to?",
    reply: "Oh, hi! I didn't see you come in. Look what I found under the couch! It's your missing sock.",
  },
  {
    label: "bad news",
    heard: "I lost my job today.",
    reply: "Oh no, I'm so sorry. That's really hard. Do you want to talk about it?",
  },
  {
    label: "map done",
    heard: "",
    reply: "<emote>proud</emote> I did it! The whole map is done. Took me three tries, though.",
  },
  {
    label: "a noise",
    heard: "",
    reply: "<emote>startled. jumps back</emote> Whoa! What was that? Oh. It's just the cat.",
  },
];

/** @param {unknown} err */
const message = (err) => (err instanceof Error ? err.message : String(err));

/** @param {number} n */
const plural = (n) => `${n} sentence${n === 1 ? "" : "s"}`;

/** Every tag performed with `beat`, its closing ones last. @param {Beat} beat */
const tagsOf = (beat) => [...beat.emotes, ...beat.closing];

/** @param {number} ms @param {AbortSignal} signal @returns {Promise<void>} */
const sleep = (ms, signal) =>
  new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => (clearTimeout(timer), resolve()), { once: true });
  });

/**
 * @template {SpeechTake} T the page's take
 * @param {(name: string) => HTMLElement} el the page's data-el lookup
 * @param {{
 *   makeTake: (made: Made, beat: Beat, prompt: string) => T,
 *   presetTake: (name: string) => T,
 *   play: (take: T) => void,
 *   status: (text: string, kind?: "" | "busy" | "ok" | "err") => void,
 *   log: (text: string, cls?: "ok" | "err") => void,
 *   robotPlanner: ReturnType<typeof import("./planner.js").createRobotPlanner>,
 *   gemini: import("./judge.js").GeminiConfig,
 *   serverUrl: () => string,
 *   ros: import("../rosClient.js").RosClient,
 *   prefs: { reply: string, heard: string, source: string, effort: string, voice: boolean },
 *   savePrefs: () => void,
 * }} deps makeTake turns a planner's answer into a playable take for `beat`
 */
export function createSpeechCard(el, deps) {
  /**
   * A sentence of the current reply and what became of it.
   * @typedef {Beat & {
   *   status: "unplanned" | "waiting" | "planning" | "ready" | "fallback",
   *   take: T | null, used: string, source: string, detail: string, failures: string[],
   *   ms: number | null, cuedAt: number | null, lateMs: number | null, missed: boolean,
   *   ready: Promise<T | null>, settle: (take: T | null) => void,
   * }} BeatState
   */
  /** @typedef {{ key: string, beats: BeatState[], ctrl: AbortController }} Run */
  const { prefs } = deps;
  const replyBox = /** @type {HTMLTextAreaElement} */ (el("reply"));
  const heardBox = /** @type {HTMLInputElement} */ (el("heard"));
  const sourceBox = /** @type {HTMLSelectElement} */ (el("speechSource"));
  const effortBox = /** @type {HTMLSelectElement} */ (el("effort"));
  const voiceBox = /** @type {HTMLInputElement} */ (el("voice"));
  const canSpeak = "speechSynthesis" in window;

  for (const [value, label] of SOURCES) sourceBox.add(new Option(label, value));
  for (const effort of EFFORTS) effortBox.add(new Option(effort, effort));
  replyBox.value = prefs.reply;
  heardBox.value = prefs.heard;
  sourceBox.value = prefs.source;
  effortBox.value = prefs.effort;
  voiceBox.checked = prefs.voice && canSpeak;
  voiceBox.disabled = !canSpeak;
  if (!canSpeak) voiceBox.parentElement?.setAttribute("title", "this browser has no speech synthesis");

  /** @type {Run | null} */
  let run = null;
  /** @type {AbortController | null} */
  let show = null;
  // The sentence whose audio started last, and in which show: like the robot's newest cue, it takes
  // a late clip until the next sentence starts, even after the show's last sentence.
  /** @type {{ show: AbortController, index: number } | null} */
  let current = null;
  /** Chrome drops the end event of an utterance nothing references. @type {SpeechSynthesisUtterance | null} */
  let voicing = null;
  let voiceUnlocked = false;

  const runKey = () => JSON.stringify([replyBox.value, heardBox.value, sourceBox.value, effortBox.value]);

  /** @param {Beat} beat @returns {BeatState} */
  function fresh(beat) {
    /** @type {(take: T | null) => void} */
    let settle = () => {};
    const ready = new Promise((resolve) => (settle = resolve));
    return {
      ...beat,
      status: "unplanned",
      take: null,
      used: beat.prompt,
      source: "",
      detail: "",
      failures: [],
      ms: null,
      cuedAt: null,
      lateMs: null,
      missed: false,
      ready,
      settle,
    };
  }

  // The chips always show how the robot would split and prompt the text as typed.
  /** @type {BeatState[]} */
  let preview = [];
  const shown = () => run?.beats ?? preview;

  function onEdit() {
    prefs.reply = replyBox.value;
    prefs.heard = heardBox.value;
    prefs.source = sourceBox.value;
    prefs.effort = effortBox.value;
    prefs.voice = voiceBox.checked;
    deps.savePrefs();
    effortBox.hidden = !["auto", "server"].includes(sourceBox.value);
    if (run && run.key !== runKey()) {
      stop();
      run.ctrl.abort();
      run = null;
    }
    if (!run) preview = replyBeats(replyBox.value, heardBox.value.trim()).map(fresh);
    render();
  }

  // ---- planning --------------------------------------------------------------------
  /**
   * The request the robot's generate path answers for one sentence: its own director prompts
   * it (and reports the prompt), an older driver plans `prompt`. Closing tags ride along as the
   * sentence's last tags, which is what replacing its performance amounts to.
   * @param {Beat} beat
   */
  function robotRequest(beat) {
    const tags = tagsOf(beat)
      .map((e) => `<emote>${e}</emote> `)
      .join("");
    return {
      speech: `${tags}${beat.text}`,
      ...(beat.heard ? { heard: beat.heard } : {}),
      ...(beat.before ? { before: beat.before } : {}),
    };
  }

  /** @returns {{ name: string, run: (beats: BeatState[], i: number, signal: AbortSignal) => Promise<Made> }[]} */
  function chain() {
    const seed = Math.floor(Math.random() * 1e6);
    const effort = effortBox.value;
    const url = deps.serverUrl();
    const server = {
      name: `5090 server (${effort})`,
      run: (/** @type {BeatState[]} */ beats, /** @type {number} */ i, /** @type {AbortSignal} */ signal) =>
        planWithServer(beats[i].prompt, { url, seed, effort, signal }),
    };
    const robot = {
      name: "robot",
      run: (/** @type {BeatState[]} */ beats, /** @type {number} */ i, /** @type {AbortSignal} */ signal) =>
        deps.robotPlanner.generate(beats[i].prompt, { signal, extra: robotRequest(beats[i]) }),
    };
    const llm = {
      name: "browser LLM",
      run: (/** @type {BeatState[]} */ beats, /** @type {number} */ i, /** @type {AbortSignal} */ signal) =>
        planWithGemini(beats[i].prompt, {
          apiKey: deps.gemini.apiKey,
          model: deps.gemini.plannerModel,
          seed,
          signal,
        }),
    };
    const choice = sourceBox.value;
    if (choice === "server") return [server];
    if (choice === "robot") return [robot];
    if (choice === "llm") return [llm];
    if (choice === "preset") return [];
    const blocked = location.protocol === "https:" && url.startsWith("http:");
    return [
      ...(blocked ? [] : [server]),
      ...(deps.ros.state === "connected" ? [robot] : []),
      ...(deps.gemini.apiKey ? [llm] : []),
    ];
  }

  /** Plan every sentence's clip in order: the run for the text as it stands, fresh when `force`. */
  function plan(force = false) {
    const key = runKey();
    if (run && run.key === key && !force) return run;
    run?.ctrl.abort();
    const beats = replyBeats(replyBox.value, heardBox.value.trim()).map(fresh);
    for (const beat of beats) beat.status = "waiting";
    const current = { key, beats, ctrl: new AbortController() };
    run = current;
    render();
    void planAll(current);
    return current;
  }

  /** @param {Run} r */
  async function planAll(r) {
    try {
      await planEach(r);
    } finally {
      for (const beat of r.beats) beat.settle(beat.take);
    }
  }

  /** @param {Run} r */
  async function planEach(r) {
    const started = performance.now();
    const sources = chain();
    for (const [i, beat] of r.beats.entries()) {
      beat.status = "planning";
      render();
      if (!show) deps.status(`planning ${i + 1}/${r.beats.length} — “${beat.text}”…`, "busy");
      const t0 = performance.now();
      for (const source of sources) {
        const timed = timeoutSignal(r.ctrl.signal, SOURCE_TIMEOUT_MS);
        try {
          const made = await source.run(r.beats, i, timed.signal);
          beat.take = deps.makeTake(made, beat, made.prompt ?? beat.prompt);
          beat.used = made.prompt ?? beat.prompt;
          beat.source = made.source;
          beat.detail = made.detail;
          break;
        } catch (err) {
          if (r.ctrl.signal.aborted) return;
          beat.failures.push(`${source.name}: ${timed.signal.aborted ? "no answer in time" : message(err)}`);
        } finally {
          timed.done();
        }
      }
      if (r.ctrl.signal.aborted) return;
      if (!beat.take) {
        // The robot's own fallback when no planner answers: the sentence's keyword preset.
        const preset = nearestPreset(tagsOf(beat).at(-1) ?? beat.text);
        beat.take = { ...deps.presetTake(preset.name), title: beat.text };
        beat.source = "preset";
        beat.detail = `“${preset.name}” ${sources.length ? "— no planner answered" : "by keyword"}`;
      }
      beat.status = beat.failures.length && beat.source === "preset" ? "fallback" : "ready";
      beat.ms = performance.now() - t0;
      beat.settle(beat.take);
      render();
    }
    const failed = r.beats.filter((b) => b.status === "fallback").length;
    if (show) return;
    const seconds = ((performance.now() - started) / 1000).toFixed(1);
    deps.status(
      `${plural(r.beats.length)} planned in ${seconds} s` +
        (failed ? ` · ${failed} fell back to presets (see the sentences)` : ""),
      failed ? "err" : "ok",
    );
  }

  // ---- performing ----------------------------------------------------------------------
  /**
   * Speak `text` aloud, calling `onStart` when its audio starts; resolves when it ends (or the show
   * stops), or with false when no voice started it (the caller times it instead). Each utterance's
   * handlers die with it: a start reported after a stop or a fallback must not cue the sentence again.
   * @param {string} text @param {() => void} onStart @param {AbortSignal} signal @returns {Promise<boolean>}
   */
  function sayAloud(text, onStart, signal) {
    return new Promise((resolve) => {
      const utterance = new SpeechSynthesisUtterance(text);
      voicing = utterance;
      let started = false;
      /** @param {boolean} spoken */
      const finish = (spoken) => {
        clearTimeout(watchdog);
        utterance.onstart = utterance.onend = utterance.onerror = null;
        signal.removeEventListener("abort", stopped);
        resolve(spoken);
      };
      const stopped = () => finish(true);
      let watchdog = setTimeout(() => {
        speechSynthesis.cancel();
        finish(false);
      }, VOICE_START_TIMEOUT_MS);
      utterance.onstart = () => {
        started = true;
        clearTimeout(watchdog);
        // A voice that never reports its end (a suspended page) must not hold the show forever.
        watchdog = setTimeout(() => finish(true), (3 * speechSeconds(text) + 5) * 1000);
        onStart();
      };
      utterance.onend = () => finish(true);
      utterance.onerror = () => finish(started);
      signal.addEventListener("abort", stopped, { once: true });
      speechSynthesis.speak(utterance);
    });
  }

  /**
   * Sentence `i`'s audio started: its clip now, or the moment it lands unless a newer sentence
   * started first.
   * @param {BeatState} beat @param {number} i @param {AbortController} ctrl @param {Captioner | null} captioner
   */
  function cue(beat, i, ctrl, captioner) {
    if (ctrl.signal.aborted) return;
    const mine = { show: ctrl, index: i };
    current = mine;
    beat.cuedAt = performance.now();
    beat.lateMs = null;
    beat.missed = false;
    const caption = () => captioner?.caption(`“${beat.text}”`, beat.take?.clip.idea || beat.used);
    caption();
    deps.status(`speaking ${i + 1}/${shown().length} — “${beat.text}”`, "busy");
    render();
    if (beat.take) {
      deps.play(beat.take);
      return;
    }
    void beat.ready.then((take) => {
      if (ctrl.signal.aborted && current !== mine) return;
      if (!take) return;
      if (current !== mine) {
        beat.missed = true; // the robot drops a clip whose sentence was superseded, too
        render();
        return;
      }
      beat.lateMs = performance.now() - /** @type {number} */ (beat.cuedAt);
      deps.play(take);
      caption();
      render();
    });
  }

  /**
   * Perform the reply: plan what is missing, wait for the first sentence's clip (the robot
   * prefetches it while TTS synthesizes), then speak sentence by sentence.
   * @param {Captioner | null} [captioner] a recording to caption each sentence on
   * @returns {Promise<void>} once the last sentence has been spoken, or the show was stopped
   */
  async function perform(captioner = null) {
    stop();
    if (voiceBox.checked && canSpeak && !voiceUnlocked) {
      voiceUnlocked = true;
      speechSynthesis.speak(new SpeechSynthesisUtterance("")); // iOS speaks only after one inside a gesture
    }
    const r = plan();
    if (!r.beats.length) {
      deps.status("nothing to say — type what MARS says", "err");
      return;
    }
    const ctrl = new AbortController();
    show = ctrl;
    try {
      const voiced = await speakAll(r, ctrl, captioner);
      if (ctrl.signal.aborted) return;
      const voice = voiced ? "the browser's voice" : "no voice, timed by length";
      deps.status(`performed ${plural(r.beats.length)} · ${voice}`, "ok");
    } finally {
      if (show === ctrl) {
        show = null;
        voicing = null;
        render();
      }
    }
  }

  /**
   * @param {Run} r @param {AbortController} ctrl @param {Captioner | null} captioner
   * @returns {Promise<boolean>} whether the voice spoke every sentence
   */
  async function speakAll(r, ctrl, captioner) {
    const { signal } = ctrl;
    deps.status("waiting for the first sentence's clip…", "busy");
    await Promise.race([r.beats[0].ready, sleep(SOURCE_TIMEOUT_MS * 3, signal)]);
    let voiced = voiceBox.checked && canSpeak;
    if (voiced) deps.status("waiting for the voice to start…", "busy");
    for (const [i, beat] of r.beats.entries()) {
      if (signal.aborted || r.ctrl.signal.aborted) break;
      const onStart = () => cue(beat, i, ctrl, captioner);
      if (voiced && (await sayAloud(beat.text, onStart, signal))) continue;
      if (signal.aborted) break;
      if (voiced) {
        voiced = false;
        deps.log("the browser's voice never started — timing the sentences by length instead", "err");
      }
      onStart();
      await sleep(speechSeconds(beat.text) * 1000, signal);
    }
    return voiced;
  }

  /** Stop the show: the voice and any sentence still to come (the clip on stage is the player's). */
  function stop() {
    current = null;
    if (!show) return;
    show.abort();
    show = null;
    if (canSpeak) speechSynthesis.cancel();
    voicing = null;
    render();
  }

  // ---- the sentence chips ------------------------------------------------------------------
  /** @param {string} cls @param {string} text @param {string} [title] */
  function span(cls, text, title) {
    const s = document.createElement("span");
    s.className = cls;
    s.textContent = text;
    if (title) s.title = title;
    return s;
  }

  const STATE_TEXT = {
    unplanned: "not planned",
    waiting: "queued",
    planning: "planning…",
    ready: "ready",
    fallback: "fallback",
  };

  /** The cue on stage in the running show, -1 between shows. */
  const speaking = () => (show && current?.show === show ? current.index : -1);

  /** @param {BeatState} beat @param {number} i */
  function chip(beat, i) {
    const li = document.createElement("li");
    const b = document.createElement("button");
    const onStage = i === speaking();
    b.className = `exs-beat ${beat.status}${onStage ? " speaking" : ""}`;
    b.disabled = !beat.take;
    b.title = beat.take ? "play this sentence's clip" : "";
    const head = span("exs-beat-head", "");
    const state = onStage ? "speaking" : STATE_TEXT[beat.status];
    head.append(span("exs-beat-n", String(i + 1)), span("exs-beat-state", state));
    if (beat.emotes.length) head.append(span("exs-beat-tag", "tag", `emote tag: ${beat.emotes.join(" · ")}`));
    if (beat.closing.length) {
      const why = `after the reply's last sentence: ${beat.closing.join(" · ")} — replaces this sentence's performance`;
      head.append(span("exs-beat-tag", "closing tag", why));
    }
    if (beat.used !== beat.prompt) {
      const why = `the robot planned from its own prompt; the studio's was:\n${beat.prompt}`;
      head.append(span("exs-beat-tag warn", "robot's prompt", why));
    }
    const meta = [
      beat.ms != null && `${Math.round(beat.ms)} ms`,
      beat.take && `${takeDuration(beat.take).toFixed(1)} s clip`,
      beat.lateMs != null && `late +${(beat.lateMs / 1000).toFixed(1)} s`,
      beat.missed && "missed its sentence",
    ].filter(Boolean);
    head.append(span("spacer", ""), span("exs-beat-meta", meta.join(" · ")));
    b.append(head, span("exs-beat-text", beat.text), span("exs-beat-prompt", beat.used, beat.used));
    const idea = beat.take?.clip.idea;
    if (idea) b.append(span("exs-beat-idea", idea, idea));
    if (beat.source) b.append(span("exs-beat-src", `${beat.source}${beat.detail ? ` · ${beat.detail}` : ""}`));
    for (const failure of beat.failures) b.append(span("exs-beat-err", failure, failure));
    b.addEventListener("click", () => {
      if (!beat.take) return;
      stop();
      deps.play(beat.take);
    });
    li.append(b);
    return li;
  }

  function render() {
    const beats = shown();
    const list = el("beats");
    list.replaceChildren(...beats.map(chip));
    const now = list.children[speaking()];
    if (now) {
      const offset = now.getBoundingClientRect().top - list.getBoundingClientRect().top;
      if (offset < 0 || offset + now.clientHeight > list.clientHeight) list.scrollTop += offset - 4;
    }
    el("beatsInfo").textContent = beats.length
      ? `${plural(beats.length)}${run ? "" : " · not planned yet"}`
      : "type what MARS says";
  }

  // ---- wiring -------------------------------------------------------------------------------
  for (const box of [replyBox, heardBox, sourceBox, effortBox, voiceBox]) {
    box.addEventListener(box instanceof HTMLSelectElement || box === voiceBox ? "change" : "input", onEdit);
  }
  replyBox.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) {
      ev.preventDefault();
      void perform();
    }
  });
  el("generate").addEventListener("click", () => {
    stop();
    plan(true);
  });
  el("speakBtn").addEventListener("click", () => void perform());
  el("sendSpeech").addEventListener("click", () => {
    const text = replyBox.value.trim();
    if (!text) return;
    if (deps.ros.state !== "connected" || !deps.ros.publish(TTS_TOPIC, { data: text })) {
      deps.log("robot not connected — nothing sent", "err");
      return;
    }
    deps.log(`sent to the robot to say and perform: “${text.slice(0, 90)}${text.length > 90 ? "…" : ""}”`, "ok");
  });
  for (const t of TRIES) {
    const b = document.createElement("button");
    b.textContent = t.label;
    b.title = `${t.heard ? `they said: ${t.heard}\n` : ""}${t.reply}`;
    b.addEventListener("click", () => {
      replyBox.value = t.reply;
      heardBox.value = t.heard;
      onEdit();
      plan();
    });
    el("speechTries").appendChild(b);
  }
  const unadvertise = deps.ros.advertise(TTS_TOPIC, "std_msgs/msg/String");
  onEdit();

  return {
    perform,
    stop,
    focus: () => replyBox.focus(),
    destroy() {
      run?.ctrl.abort();
      stop();
      unadvertise();
    },
  };
}
