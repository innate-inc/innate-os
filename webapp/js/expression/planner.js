// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Where the studio's recipes come from: the robot's own generate path (the same
// chain the robot uses: 5090 server → its brain LLM → presets), the 5090 planner
// server directly, or Gemini straight from the browser with the frozen planner
// prompt and the checker's repair loop. Every source resolves to a Clip.

import { checkRecipe, parseClip, recipeToClip } from "./pipeline.js";

/** @typedef {import("./pipeline.js").Clip} Clip */
/** @typedef {{ clip: Clip, source: string, detail: string }} Made */

// FROZEN (CONTRACTS §8): the distilled planner is trained on exactly this text.
export const SYSTEM_PROMPT = `You plan expressive motions for MARS: a small mobile robot with a 5-joint arm and gripper on its back
(its only limb), a camera head that can only tilt up/down, and wheels that turn and roll a little.
It expresses with its whole body: the arm is its posture (tall/low, open/closed, leaning in/pulling
back, canted), the gripper is its mouth, the head is its gaze, the base is its stance.
Write a RECIPE: segments separated by |
  go D k=v ...        ease to the targets in D seconds (0.15-0.3 s = a snap)
  hold D [E=v]        stay still (optionally change energy)
  osc D ch amp per    oscillate one channel: nod (p), bob (z), sway (k), lean (a), turn (b), chatter (g); per >= 0.3 s
Channels (start: a=0 x=0 z=0 p=0 k=0 b=0 d=0 g=.15 E=.5)
  a  approach -1 pull back / recoil .. +1 lean in toward the person
  x  expand   -1 fold small, closed .. +1 arm out wide, open
  z  rise     -1 low, folded down  .. +1 tall, arm raised like a mast
  p  attend   -1 gaze down (shame, sleep, defeat) .. +1 gaze up (pride, hope, looking at a face)
  k  askew    -1..+1 canted sideways (curious, puzzled, playful); 0 = composed and square
  b  orient   base turn in degrees, -60..60 (look away, turn to face, spin)
  d  advance  base forward in metres, -.25..+.25 (step toward / step back)
  g  grip     0 closed .. 1 open (gasp, chatter, bite, yawn)
  E  energy   fast detail on top: 0 frozen, 1 calm, 3 lively, 6-10 shaking / trembling
Good motion: 3-10 s with onset -> peak -> settle; posture cues AGREE (sad = low + folded + gaze
down; proud = tall + open + gaze up); big changes of x/z/g are fast (0.2-0.5 s) like an animal's
ears; a build-up moves AGAINST its release (rise and pull back before a snap down and forward;
crouch before a jump); stillness after a burst is expressive; repeated actions repeat as beats.
Before writing numbers, think about how THIS body really moves: which direction, how fast, in what order.
Examples:
  gloomy. Everything feels grey and heavy.
    go 1.5 z=-.8 x=-.6 p=-.7 a=-.3 g=.05 E=.5 | hold 2.5 E=.3 | osc 2 k .15 2 E=.4
  a curious puppy. You tilt your head at a strange noise.
    go .4 p=.6 a=.4 k=.7 z=.2 g=.3 E=2 | hold 1 E=.5 | go .4 k=-.7 E=2 | hold 1 E=.5 | go .5 k=0
  hammering a nail. Bang, bang, bang.
    go .5 z=.3 a=.2 E=1 | go .4 z=.8 p=.4 E=1.5 | go .12 z=-.3 p=-.5 a=.6 E=8 | go .4 z=.8 p=.4 E=1.5 | go .12 z=-.3 p=-.5 a=.6 E=8 | go .6 z=0 p=0 a=0 E=1
Reply with JSON only: {"idea": "<one sentence>", "recipe": "<recipe>"}`;

const GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models";
const MAX_REPAIRS = 2;
const GENERATE_REQ_TOPIC = "/brain/express/generate_req";
const GENERATE_RES_TOPIC = "/brain/express/generate_res";
const STRING_TYPE = "std_msgs/msg/String";

/** @param {string} text */
function parseRecipeJson(text) {
  const body = text.trim().replace(/^```(?:json)?\s*|\s*```$/g, "");
  const data = JSON.parse(body);
  if (typeof data?.recipe !== "string") throw new Error("planner reply has no recipe");
  return { idea: String(data.idea ?? ""), recipe: data.recipe.trim() };
}

/**
 * Gemini plans in the browser: frozen prompt, JSON reply, and up to two repair
 * turns carrying the checker's error.
 * @param {string} prompt
 * @param {{ apiKey: string, model: string, seed?: number, signal?: AbortSignal }} cfg
 * @returns {Promise<Made>}
 */
export async function planWithGemini(prompt, { apiKey, model, seed = 0, signal }) {
  /** @type {{ role: string, parts: { text: string }[] }[]} */
  const contents = [{ role: "user", parts: [{ text: prompt }] }];
  for (let attempt = 0; attempt <= MAX_REPAIRS; attempt++) {
    const res = await fetch(`${GEMINI_URL}/${encodeURIComponent(model)}:generateContent`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-goog-api-key": apiKey },
      body: JSON.stringify({
        systemInstruction: { parts: [{ text: SYSTEM_PROMPT }] },
        contents,
        generationConfig: {
          temperature: 0.7,
          responseMimeType: "application/json",
          thinkingConfig: { thinkingLevel: "minimal" },
        },
      }),
      signal,
    });
    if (!res.ok) throw new Error(`Gemini HTTP ${res.status}: ${(await res.text()).slice(0, 160)}`);
    const data = await res.json();
    const text = (data?.candidates?.[0]?.content?.parts ?? []).map((/** @type {any} */ p) => p?.text ?? "").join("");
    const { idea, recipe } = parseRecipeJson(text);
    const error = checkRecipe(recipe);
    if (!error) {
      const { clip } = recipeToClip(recipe, { name: prompt, prompt, idea, seed });
      return {
        clip,
        source: "browser LLM",
        detail: `${model}${attempt ? ` · ${attempt} repair${attempt > 1 ? "s" : ""}` : ""}`,
      };
    }
    contents.push(
      { role: "model", parts: [{ text }] },
      { role: "user", parts: [{ text: `That recipe is invalid: ${error}. Reply with the corrected JSON only.` }] },
    );
  }
  throw new Error(`no valid recipe after ${MAX_REPAIRS} repairs`);
}

/** The planner server's generate endpoint from a base URL or the full one. @param {string} url */
export const denseUrl = (url) => (/\/generate-dense\/?$/.test(url) ? url : `${url.replace(/\/+$/, "")}/generate-dense`);

/**
 * The 5090 planner + generator (POST /generate-dense). Uses its generated
 * clip when it sends one, else expands its recipe here.
 * @param {string} prompt @param {{ url: string, seed?: number, signal?: AbortSignal }} cfg @returns {Promise<Made>}
 */
export async function planWithServer(prompt, { url, seed = 0, signal }) {
  if (location.protocol === "https:" && url.startsWith("http:")) {
    throw new Error("this page is https, the server http: the browser blocks it — use the robot source");
  }
  const res = await fetch(denseUrl(url), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt, n: 1, seed, effort: "medium" }),
    signal,
  });
  if (!res.ok) throw new Error(`server HTTP ${res.status}: ${(await res.text()).slice(0, 160)}`);
  const data = await res.json();
  const raw = data?.clips?.[0];
  if (raw) {
    const clip = parseClip({ name: prompt, prompt, idea: data.idea, recipe: data.recipe, ...raw });
    const total = Number(data?.timing_ms?.total);
    return {
      clip,
      source: "server",
      detail: `planner + generator${Number.isFinite(total) ? ` · ${Math.round(total)} ms on the 5090` : ""}`,
    };
  }
  const recipe = String(data?.recipe ?? "");
  const error = checkRecipe(recipe);
  if (error) throw new Error(`server recipe invalid: ${error}`);
  const { clip } = recipeToClip(recipe, { name: prompt, prompt, idea: String(data.idea ?? ""), seed });
  return { clip, source: "server", detail: "planner · local liveliness" };
}

/**
 * The robot's generate path: {id, prompt} on generate_req, answered by id on
 * generate_res (the robot's chain picks the source). One subscription for the
 * page's life, so a reply can't beat a fresh subscribe to the socket.
 * @param {import("../rosClient.js").RosClient} ros
 */
export function createRobotPlanner(ros) {
  /** @type {Map<string, { resolve: (made: Made) => void, reject: (err: Error) => void, prompt: string }>} */
  const pending = new Map();
  const unadvertise = ros.advertise(GENERATE_REQ_TOPIC, STRING_TYPE);
  const unsubscribe = ros.subscribe(
    GENERATE_RES_TOPIC,
    (/** @type {{ data?: string }} */ msg) => {
      /** @type {any} */
      let reply;
      try {
        reply = JSON.parse(msg?.data ?? "");
      } catch {
        return;
      }
      const waiter = pending.get(String(reply?.id));
      if (!waiter) return;
      pending.delete(String(reply.id));
      if (reply.error) {
        waiter.reject(new Error(`robot: ${reply.error}`));
        return;
      }
      try {
        const clip = parseClip({ prompt: waiter.prompt, ...reply.clip });
        waiter.resolve({ clip, source: "robot", detail: String(reply.source ?? "") });
      } catch (err) {
        waiter.reject(err instanceof Error ? err : new Error(String(err)));
      }
    },
    undefined,
    STRING_TYPE,
  );

  return {
    /** @param {string} prompt @param {{ timeoutMs?: number, signal?: AbortSignal }} [opts] @returns {Promise<Made>} */
    generate(prompt, { timeoutMs = 20_000, signal } = {}) {
      if (ros.state !== "connected") return Promise.reject(new Error("robot not connected"));
      // randomUUID exists only in secure contexts; the robot also serves the app on plain http.
      const id = crypto.randomUUID?.() ?? `web-${Date.now()}-${Math.random()}`;
      return new Promise((resolve, reject) => {
        const done = () => {
          clearTimeout(timer);
          pending.delete(id);
          signal?.removeEventListener("abort", onAbort);
        };
        const onAbort = () => {
          done();
          reject(new DOMException("aborted", "AbortError"));
        };
        const timer = setTimeout(() => {
          done();
          reject(new Error("robot did not answer (is the expressive driver running?)"));
        }, timeoutMs);
        signal?.addEventListener("abort", onAbort);
        pending.set(id, {
          prompt,
          resolve: (made) => {
            done();
            resolve(made);
          },
          reject: (err) => {
            done();
            reject(err);
          },
        });
        ros.publish(GENERATE_REQ_TOPIC, { data: JSON.stringify({ id, prompt }) });
      });
    },
    destroy() {
      for (const waiter of pending.values()) waiter.reject(new Error("studio closed"));
      pending.clear();
      unsubscribe();
      unadvertise();
    },
  };
}
