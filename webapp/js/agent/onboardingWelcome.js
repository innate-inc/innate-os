// @ts-check
// Deterministic scripted actions for onboarding: exact /brain/tts lines and
// sequential /execute_skill goals. Motion gated on browser TTS playback start.
// A "ui" action carries its own effect, which is how a tour step reveals the
// panel it is pointing at without this runner knowing anything about the page.

import {
  AVAILABLE_SKILLS_TOPIC,
  CANCEL_SKILL_SERVICE,
  EXECUTE_SKILL_ACTION,
  EXECUTE_SKILL_ACTION_TYPE,
  TTS_TOPIC,
} from "../constants.js";
import { onTtsPlaybackStart } from "../ttsAudio.js";

// Matches the panel reveal transition in app.css.
const UI_SETTLE_MS = 260;
// A spoken line normally starts playing within ~2s (synthesis plus transfer).
// Past this the script stops waiting and carries on: playback start is a
// convenience for syncing gestures, and several ordinary situations never
// deliver it at all -- another tab holds the shared speaker lock, autoplay is
// blocked, a clip fails to decode. None of those should strand the tour.
const SPEECH_START_TIMEOUT_MS = 6000;

/**
 * @typedef {{
 *   type: "speak",
 *   text: string,
 *   queue?: boolean,
 * } | {
 *   type: "skill",
 *   name: string,
 *   inputs?: Record<string, unknown>,
 *   afterSpeechStart?: boolean,
 * } | {
 *   type: "ui",
 *   apply: () => void,
 * }} ScriptedAction
 */

/** @param {import("../rosClient.js").RosClient} rosClient */
export function createScriptedActionRunner(rosClient) {
  /** @type {any[]} */
  let skills = [];
  /** @type {(() => void) | null} */
  let cancelActive = null;
  /** @type {(() => void) | null} */
  let resolveSpeechStart = null;
  let generation = 0;

  const unsubSkills = rosClient.subscribe(
    AVAILABLE_SKILLS_TOPIC,
    (message) => {
      skills = Array.isArray(message?.skills) ? message.skills : [];
    },
    undefined,
    "brain_messages/msg/AvailableSkills",
  );

  const stopPlaybackListener = onTtsPlaybackStart(() => {
    const resolve = resolveSpeechStart;
    resolveSpeechStart = null;
    resolve?.();
  });

  /** @param {number} id */
  function waitForSpeechStart(id) {
    return new Promise((/** @type {(value?: void) => void} */ resolve) => {
      if (id !== generation) {
        resolve();
        return;
      }
      let settled = false;
      const finish = () => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (resolveSpeechStart === finish) resolveSpeechStart = null;
        resolve();
      };
      const timer = setTimeout(() => {
        console.warn("[onboarding] no speech playback heard — continuing without it");
        finish();
      }, SPEECH_START_TIMEOUT_MS);
      resolveSpeechStart = finish;
    });
  }

  /** @param {string} name @param {Record<string, unknown>} inputs @param {number} id */
  async function runSkill(name, inputs, id) {
    if (id !== generation) return;
    const skillId = await waitForSkillId(name, id);
    if (!skillId || id !== generation) return;
    const action = rosClient.sendActionGoal(
      EXECUTE_SKILL_ACTION,
      EXECUTE_SKILL_ACTION_TYPE,
      { skill_type: skillId, inputs: JSON.stringify(inputs) },
    );
    cancelActive = action.cancel;
    try {
      await action.promise;
    } catch (error) {
      if (id === generation) console.warn(`[onboarding] '${name}' failed`, error);
    } finally {
      if (cancelActive === action.cancel) cancelActive = null;
    }
  }

  /** @param {string} name @param {number} id */
  async function waitForSkillId(name, id) {
    const existing = findSkillId(skills, name);
    if (existing) return existing;
    const deadline = Date.now() + 8000;
    while (id === generation && Date.now() < deadline) {
      await sleep(100);
      const found = findSkillId(skills, name);
      if (found) return found;
    }
    if (id === generation) console.warn(`[onboarding] '${name}' is unavailable`);
    return "";
  }

  /**
   * @param {ScriptedAction[]} actions
   * @returns {Promise<void>}
   */
  async function run(actions) {
    cancel();
    const id = ++generation;
    let heardSpeech = false;

    for (const action of actions) {
      if (id !== generation) return;
      if (action.type === "speak") {
        // Utterances queue on the robot and play in order, so a queued line is
        // synthesizing while the previous one is still being heard. Waiting on
        // every line instead would put the whole synthesis delay in the gap
        // between them.
        if (action.queue) {
          rosClient.publish(TTS_TOPIC, { data: action.text });
          continue;
        }
        const speechStarted = waitForSpeechStart(id);
        rosClient.publish(TTS_TOPIC, { data: action.text });
        await speechStarted;
        heardSpeech = id === generation;
        continue;
      }
      if (action.type === "ui") {
        action.apply();
        // Let the reveal's CSS transition start before the next line lands, so
        // the panel is on screen while the robot talks about it.
        await sleep(UI_SETTLE_MS);
        continue;
      }
      if (action.type === "skill") {
        if (action.afterSpeechStart && !heardSpeech) {
          await waitForSpeechStart(id);
          heardSpeech = id === generation;
        }
        await runSkill(action.name, action.inputs ?? {}, id);
      }
    }
  }

  function cancel() {
    generation++;
    const resolve = resolveSpeechStart;
    resolveSpeechStart = null;
    resolve?.();
    cancelActive?.();
    cancelActive = null;
    void rosClient.callService(CANCEL_SKILL_SERVICE, {}).catch(() => {});
  }

  return {
    run,
    cancel,
    destroy() {
      cancel();
      stopPlaybackListener();
      unsubSkills();
    },
  };
}

/** @deprecated Use createScriptedActionRunner */
export const createOnboardingWelcome = createScriptedActionRunner;

/** @param {any[]} skills @param {string} name */
function findSkillId(skills, name) {
  const expected = normalize(name);
  const match = skills.find((skill) =>
    [skill?.id, skill?.name].some((value) => normalize(String(value ?? "").split("/").at(-1) ?? "") === expected),
  );
  return typeof match?.id === "string" ? match.id : "";
}

/** @param {string} value */
function normalize(value) {
  return value.trim().toLowerCase().replace(/[\s-]+/g, "_");
}

/** @param {number} ms */
function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
