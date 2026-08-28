// @ts-check
// Versioned declarative onboarding engine. Steps own fixed dialogue, completion
// events, entry actions, and next links. Progress is {version, stepId} only.
//
// The tour runs entirely from the browser and never listens to what the user
// says: a step that waits on the user completes when they hold the mic, however
// long they talk and whatever words they use. That keeps the brain out of it —
// no agent, no STT, no transcript to match — so the only robot-side traffic is
// speech and skills, plus one custom-input event at the handoff that tells the
// agent it is joining a conversation already in progress.

import { CUSTOM_INPUT_TOPIC } from "../constants.js";

const ONBOARDING_VERSION = 3;
const STORAGE_KEY = "innate.agentOnboarding";
const LEGACY_STEP_KEY = "innate.agentOnboardingStep";
const LEGACY_COMPLETE_KEY = "innate.agentOnboardingComplete";
const RESET_EVENT = "innate:agent-onboarding-reset";
// A hold this long reads as "the user said something". Comfortably above the
// mic control's 350ms short-click threshold, so a stray click cannot advance
// the tour, and short enough that holding never feels like a wait.
const MIN_HOLD_MS = 600;
// How long a prompt may go unanswered before offering a way past it. Denied
// microphone permission never produces a hold at all, and a tour that cannot be
// skipped would strand the page on its first step.
const SKIP_OFFER_MS = 25000;
// Where the arm reaches to point at a panel, in base_link metres: x forward,
// y left, z up. The onboarding camera sits almost directly in front of the
// robot (eye at -3.5,-3.5 looking at a spawn of -4.34,-0.17 facing -Y), so the
// view is mirrored — reaching to the robot's left shows up on the viewer's
// right. SIDE_LEFT/SIDE_RIGHT are named for where the panel is on screen, and
// carry the flip so the call sites do not have to think about it.
const POINT_REACH = { x: 0.25, z: 0.3, duration: 2 };
const SIDE_LEFT = -0.2;
const SIDE_RIGHT = 0.2;

// Spoken as two utterances, not one: synthesis time scales with the text, so a
// short opener is heard far sooner, and the longer line is generated while it
// plays. Same reason the agent's own replies stream a sentence at a time.
const WELCOME_LINES = [
  "Welcome to the simulator!",
  "I'll be here to show you some of the main things we can do together!",
];

export const WELCOME_DIALOGUE = WELCOME_LINES.join(" ");

/**
 * Completion event kinds reserved for later lessons:
 * - hold: the user held the mic (content ignored)
 * - action: entry scripted runner finished
 * - skill_success / sim_event / user_action: extension points (unused yet)
 * - complete: terminal handoff step
 *
 * @typedef {"await_hello" | "welcome" | "await_go" | "tour_cameras" | "tour_telemetry" | "tour_chat" | "complete"} OnboardingStepId
 * @typedef {"hold" | "action" | "skill_success" | "sim_event" | "user_action" | "complete"} CompletionKind
 * @typedef {{
 *   id: OnboardingStepId,
 *   instruction?: string,
 *   dialogue?: string,
 *   completeOn: CompletionKind,
 *   reveal?: string,
 *   recap?: string,
 *   actions?: (reveal: () => void) => import("./onboardingWelcome.js").ScriptedAction[],
 *   next: OnboardingStepId | null,
 * }} OnboardingStepDef
 */

/** @type {Record<OnboardingStepId, OnboardingStepDef>} */
const STEPS = {
  await_hello: {
    id: "await_hello",
    // The words are a suggestion, not a condition — holding the mic is the gate.
    instruction: 'Hold the mic and say <strong>“Hello MARS”</strong>',
    completeOn: "hold",
    next: "welcome",
  },
  welcome: {
    id: "welcome",
    dialogue: WELCOME_DIALOGUE,
    completeOn: "action",
    recap: "greeted them and waved",
    // No approach step: the 0.2m nudge this used to open with cost seven to
    // sixteen seconds of silence before the first word, and it displaced the
    // robot a little further every run until it wedged against something and
    // started failing outright. Appearing and speaking is the moment; creeping
    // forward first was never part of it.
    actions: () => [
      { type: "speak", text: WELCOME_LINES[0] },
      // Queued the moment the opener starts playing, so its synthesis overlaps
      // the opener instead of landing in the pause after it.
      { type: "speak", text: WELCOME_LINES[1], queue: true },
      { type: "skill", name: "head_emotion", inputs: { emotion: "excited", repeat: 5 }, afterSpeechStart: true },
      { type: "skill", name: "wave", inputs: {} },
    ],
    next: "await_go",
  },
  await_go: {
    id: "await_go",
    instruction: 'Hold the mic and say <strong>“Let’s go”</strong>',
    completeOn: "hold",
    next: "tour_cameras",
  },
  tour_cameras: {
    id: "tour_cameras",
    dialogue: "Up here are my cameras. That's how I see the room — and what I look at when you ask me about something.",
    completeOn: "action",
    reveal: "cameras",
    recap: "showed them the camera views",
    actions: (reveal) => pointOut(SIDE_LEFT, reveal, STEPS.tour_cameras.dialogue ?? "", "happy"),
    next: "tour_telemetry",
  },
  tour_telemetry: {
    id: "tour_telemetry",
    dialogue: "This panel is how I'm doing — where I am, how I'm moving, how much battery I have left.",
    completeOn: "action",
    reveal: "telemetry",
    recap: "showed them the telemetry panel",
    actions: (reveal) => pointOut(SIDE_RIGHT, reveal, STEPS.tour_telemetry.dialogue ?? "", "agreeing"),
    next: "tour_chat",
  },
  tour_chat: {
    id: "tour_chat",
    dialogue: "And this is where we talk. Hold the microphone, or type — either way, I'm listening.",
    completeOn: "action",
    reveal: "chat",
    recap: "showed them the chat panel",
    actions: (reveal) => pointOut(SIDE_RIGHT, reveal, STEPS.tour_chat.dialogue ?? "", "very_happy"),
    next: "complete",
  },
  complete: {
    id: "complete",
    completeOn: "complete",
    next: null,
  },
};

/** Step order, which is also reveal order: resuming re-applies every earlier reveal. */
/** @type {OnboardingStepId[]} */
const STEP_ORDER = [
  "await_hello",
  "welcome",
  "await_go",
  "tour_cameras",
  "tour_telemetry",
  "tour_chat",
  "complete",
];

/** @type {string[]} */
const REVEAL_TARGETS = ["cameras", "telemetry", "chat"];

/**
 * One tour beat: reach toward the panel, reveal it, say the line, react, lower
 * the arm. Pointing is the arm, not the base — turning the whole robot reads as
 * "it spun round", and it leaves the robot facing somewhere new for the step
 * after it.
 *
 * @param {number} sideY signed base_link y: SIDE_LEFT or SIDE_RIGHT
 * @param {() => void} reveal
 * @param {string} text
 * @param {string} emotion
 * @returns {import("./onboardingWelcome.js").ScriptedAction[]}
 */
function pointOut(sideY, reveal, text, emotion) {
  return [
    { type: "skill", name: "arm_move_to_xyz", inputs: { ...POINT_REACH, y: sideY } },
    { type: "ui", apply: reveal },
    { type: "speak", text },
    { type: "skill", name: "head_emotion", inputs: { emotion }, afterSpeechStart: true },
    { type: "skill", name: "arm_rest_position", inputs: {} },
  ];
}

/**
 * @param {HTMLElement} root
 * @param {import("../rosClient.js").RosClient} rosClient
 * @param {{
 *   runner: {
 *     run: (actions: import("./onboardingWelcome.js").ScriptedAction[]) => Promise<void>,
 *     cancel: () => void,
 *   },
 *   onHandoff?: () => Promise<void> | void,
 *   onResetWorld?: () => void,
 * }} options
 */
export function createAgentOnboarding(root, rosClient, options) {
  const { runner, onHandoff, onResetWorld } = options;
  /** @type {OnboardingStepId} */
  let stepId = loadStepId();
  /** @type {Set<(step: OnboardingStepId) => void>} */
  const stepListeners = new Set();
  let entryToken = 0;
  let destroyed = false;
  /** @type {Set<string>} */
  const revealed = new Set();
  /** @type {ReturnType<typeof setTimeout> | null} */
  let skipTimer = null;
  /** When the current mic hold started, 0 when the mic is not held. */
  let micDownAt = 0;
  root.classList.add("agent-onboarding-enabled");

  // Advertised up front, not at first use: an unadvertised topic resolves its
  // type from an existing publisher, and the first message can be dropped
  // before rosbridge finishes wiring it up.
  const unadvertiseCustom = rosClient.advertise(CUSTOM_INPUT_TOPIC, "std_msgs/msg/String");

  const nudge = document.createElement("p");
  nudge.className = "agent-onboarding-nudge";
  root.append(nudge);

  const skip = document.createElement("button");
  skip.type = "button";
  skip.className = "agent-onboarding-skip";
  skip.textContent = "Skip intro";
  skip.hidden = true;
  skip.addEventListener("click", () => {
    void enter("complete");
  });
  root.append(skip);

  /** @param {OnboardingStepId} id */
  function waitsForHold(id) {
    return STEPS[id]?.completeOn === "hold";
  }

  function render() {
    const active = stepId !== "complete";
    const awaiting = waitsForHold(stepId);
    root.classList.toggle("agent-onboarding-active", active);
    // Distinct from the per-step classes: the robot appears at the welcome and
    // stays on screen for the whole tour, so the stage cannot key off one step.
    root.classList.toggle("agent-onboarding-staged", stepId !== "await_hello");
    root.classList.toggle("agent-onboarding-awaiting", awaiting);
    for (const id of STEP_ORDER) {
      root.classList.toggle(`agent-onboarding-${id}`, stepId === id);
    }
    for (const target of REVEAL_TARGETS) {
      root.classList.toggle(`agent-onboarding-show-${target}`, revealed.has(target));
    }
    nudge.innerHTML = STEPS[stepId]?.instruction ?? "";
    nudge.setAttribute("aria-hidden", String(!awaiting));
    for (const listener of stepListeners) listener(stepId);
  }

  /** Reveals every panel introduced at or before `upTo`, so a resumed step keeps its predecessors. */
  /** @param {OnboardingStepId} upTo */
  function syncRevealsThrough(upTo) {
    const limit = STEP_ORDER.indexOf(upTo);
    for (const id of STEP_ORDER.slice(0, limit)) {
      const target = STEPS[id].reveal;
      if (target) revealed.add(target);
    }
  }

  function persist() {
    storageSet(STORAGE_KEY, JSON.stringify({ version: ONBOARDING_VERSION, stepId }));
  }

  function armSkipOffer() {
    if (skipTimer !== null) clearTimeout(skipTimer);
    const armedFor = stepId;
    skipTimer = setTimeout(() => {
      skipTimer = null;
      if (destroyed || stepId !== armedFor) return;
      skip.hidden = false;
    }, SKIP_OFFER_MS);
  }

  /** @param {OnboardingStepId} nextId */
  async function enter(nextId) {
    if (destroyed) return;
    const crossingIntoComplete = nextId === "complete" && stepId !== "complete";
    stepId = nextId;
    micDownAt = 0;
    syncRevealsThrough(nextId);
    persist();
    render();
    const token = ++entryToken;
    if (skipTimer !== null) {
      clearTimeout(skipTimer);
      skipTimer = null;
    }
    skip.hidden = true;
    if (nextId === "complete") {
      runner.cancel();
      if (crossingIntoComplete) {
        await onHandoff?.();
        if (token !== entryToken || destroyed) return;
        publishHandoffContext(rosClient);
      }
      return;
    }
    if (waitsForHold(nextId)) {
      // A fresh tour starts from spawn. This is what makes the rail's Reset
      // onboarding work from any page: the button clears storage and navigates
      // here, and the newly mounted engine enters await_hello and asks for the
      // world reset itself — the engine that heard the click is already gone.
      if (nextId === "await_hello") onResetWorld?.();
      armSkipOffer();
      return;
    }
    if (nextId === "welcome") {
      // The robot becomes visible here, so this is where it has to be standing
      // at spawn — and it is late enough that the stage channel is connected,
      // which it is not when the page first builds.
      onResetWorld?.();
    }
    const step = STEPS[nextId];
    if (step.completeOn !== "action" || !step.actions) return;
    await runner.run(step.actions(() => {
      if (step.reveal) revealed.add(step.reveal);
      render();
    }));
    if (token !== entryToken || destroyed || stepId !== nextId) return;
    await advance();
  }

  async function advance() {
    const step = STEPS[stepId];
    if (!step?.next) return;
    await enter(step.next);
  }

  async function reset() {
    runner.cancel();
    entryToken++;
    revealed.clear();
    skip.hidden = true;
    storageRemove(STORAGE_KEY);
    storageRemove(LEGACY_STEP_KEY);
    storageRemove(LEGACY_COMPLETE_KEY);
    await enter("await_hello");
  }

  const onReset = () => {
    void reset();
  };
  window.addEventListener(RESET_EVENT, onReset);

  void enter(stepId);

  return {
    getStep() {
      return stepId;
    },
    isComplete() {
      return stepId === "complete";
    },
    /**
     * True while the tour still owns the mic button, which is what keeps
     * holding it from starting the agent underneath the tour.
     */
    async ensureListening() {
      return stepId !== "complete";
    },
    /** The mic button went down: start timing the hold. */
    noteMicDown() {
      micDownAt = Date.now();
    },
    /** The mic button came up: a long enough hold answers a waiting step. */
    noteMicUp() {
      const heldFor = micDownAt ? Date.now() - micDownAt : 0;
      micDownAt = 0;
      if (heldFor < MIN_HOLD_MS || !waitsForHold(stepId)) return;
      void advance();
    },
    /** @param {(step: OnboardingStepId) => void} listener */
    onStep(listener) {
      stepListeners.add(listener);
      listener(stepId);
      return () => stepListeners.delete(listener);
    },
    destroy() {
      destroyed = true;
      entryToken++;
      runner.cancel();
      window.removeEventListener(RESET_EVENT, onReset);
      if (skipTimer !== null) clearTimeout(skipTimer);
      stepListeners.clear();
      nudge.remove();
      skip.remove();
      root.classList.remove(
        "agent-onboarding-enabled",
        "agent-onboarding-active",
        "agent-onboarding-staged",
        "agent-onboarding-awaiting",
        ...STEP_ORDER.map((id) => `agent-onboarding-${id}`),
        ...REVEAL_TARGETS.map((target) => `agent-onboarding-show-${target}`),
      );
      unadvertiseCustom();
    },
  };
}

export function resetAgentOnboarding() {
  storageRemove(STORAGE_KEY);
  storageRemove(LEGACY_STEP_KEY);
  storageRemove(LEGACY_COMPLETE_KEY);
  window.dispatchEvent(new Event(RESET_EVENT));
}

/** @returns {OnboardingStepId} */
function loadStepId() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (parsed?.version === ONBOARDING_VERSION && isStepId(parsed?.stepId)) {
        return parsed.stepId;
      }
      // A finished older tour stays finished; an unfinished one restarts, since
      // its step ids no longer describe this version's lesson order.
      if (parsed?.stepId === "complete") return "complete";
    }
    const legacy = localStorage.getItem(LEGACY_STEP_KEY);
    if (legacy === "greeted") return "welcome";
    if (legacy === "complete") return "complete";
    if (legacy === "hello") return "await_hello";
    if (localStorage.getItem(LEGACY_COMPLETE_KEY)) return "complete";
  } catch {
    // Fall through to the first lesson.
  }
  return "await_hello";
}

/** @param {unknown} value @returns {value is OnboardingStepId} */
function isStepId(value) {
  return typeof value === "string" && Object.prototype.hasOwnProperty.call(STEPS, value);
}

/**
 * Tell the agent what it just did, so it continues the conversation instead of
 * opening a new one. Only meaningful once the brain is active — custom input is
 * dropped while it is not — so this runs after the directive has been set.
 *
 * @param {import("../rosClient.js").RosClient} rosClient
 */
function publishHandoffContext(rosClient) {
  const recap = STEP_ORDER.map((id) => STEPS[id].recap).filter(Boolean).join(", ");
  rosClient.publish(CUSTOM_INPUT_TOPIC, {
    data: JSON.stringify({
      input_device: "onboarding",
      event: "onboarding_complete",
      summary:
        `You have just finished welcoming this user to the simulator. You ${recap}. ` +
        "They are still here and the conversation is already under way — carry on from " +
        "that point, and do not greet them or introduce yourself again.",
    }),
  });
}

/** @param {string} key @param {string} value */
function storageSet(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    // In-memory step still advances for this page view.
  }
}

/** @param {string} key */
function storageRemove(key) {
  try {
    localStorage.removeItem(key);
  } catch {
    // Reset still applies through the event for the current page.
  }
}
