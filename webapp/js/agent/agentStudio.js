// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Agent detail: who the selected agent is, what it can do, and — while the
// Nowhere story runs — the one place that mirrors what the world is asking for.
//
// Outside the story it is the form for an agent: its prompt and its skills, saved
// as the agent's file by the brain. Innate agents and files edited in code are
// shown read-only; "Create agent" in the picker opens the same form empty.
//
// The story owns the UI only while the world says it is running (`storyRunning`).
// Selecting the story's agent by hand is not the story: the person keeps the rail,
// the scene setup and the challenges, and nothing hides behind a mode they cannot leave.

import { closeIn, cue } from "./cue.js";
import { createOfferDeck } from "./offerDeck.js";
import { personaCard, skillCard } from "./storyCards.js";

const STORY_AGENT = "void_agent";
const SKIPPED_AGENT = "demo_agent"; // who the robot is once the story is skipped
const SKIP_KEY = "innate.nowhere.skip.v1";
// Learning that the 3D view drags is a once-ever lesson, not a once-per-run one.
const DRAG_HINT_KEY = "innate.nowhere.draghint.v1";
const ARMED_KEY = "innate.nowhere.armed";
// Set by the rail's "Play the intro" for an Agent page that is still mounting.
export const PLAY_INTRO_KEY = "innate.nowhere.play";
const UNMENTIONED_SKILLS = new Set(["innate-os/open_gripper"]); // never part of the story's arc
const WAVE = "innate-os/wave";
const MEMORY = "innate-os/search_memory";
const GRADUATION_MAX_WAIT_MS = 25_000;
// The robot asks for its next skill in its own time, and sometimes takes a while. The grant
// waits for it that long, then offers itself anyway: nobody should be stuck watching.
const GRANT_GRACE_MS = 15_000;
// How long a grant waits for the brain to confirm the new toolset before announcing it.
const GRANT_WAIT_MS = 4_000;
// How long a persona choice has to come back as the act advancing before it is worth saying
// the world never took it.
const PERSONA_ECHO_MS = 8_000;
// A skill that just landed flashes in the panel this long: the person sees where it went.
const LANDED_MS = 2_000;

/** Resolve once `ready()` holds, or when the wait runs out — whichever comes first.
 * @param {() => boolean} ready @param {number} timeoutMs */
function held(ready, timeoutMs) {
  if (ready()) return Promise.resolve();
  return new Promise((resolve) => {
    const deadline = Date.now() + timeoutMs;
    const poll = setInterval(() => {
      if (!ready() && Date.now() < deadline) return;
      clearInterval(poll);
      resolve(undefined);
    }, 200);
  });
}

// A grant is a turn for the brain, not only a toolset change: the chip says it out loud.
const GRANT_LINES = /** @type {Record<string, string>} */ ({
  "innate-os/head_emotion": "Granted: the HeadEmotion skill. Use it.",
  "innate-os/turn_in_place": "Granted: the TurnInPlace skill. Have a look around.",
  "innate-os/pick_any_object": "Granted: the PickAnyObject skill. Pick it up.",
  "innate-os/navigate_to_position": "Granted: the NavigateToPosition skill. Go to the door.",
  "innate-os/search_memory": "Granted: the SearchMemory skill. Think back.",
});
// What the world does at the top of an act, said to the visitor (not to the brain:
// the act's own brief already tells the robot what changed).
const ACT_OPENERS = /** @type {Record<string, string>} */ ({
  "Pick up the cube": "Something just landed on the floor in front of you.",
  "Go through the door": "A door. Standing on its own, right there.",
});

/** @param {string} id */
const leaf = (id) => id.split("/").at(-1) ?? id;
// Skills are shown by their class names on purpose: the vocabulary the visitor takes away.
/** @param {string} id */
const skillLabel = (id) =>
  leaf(id)
    .split(/[-_]+/)
    .filter(Boolean)
    .map((part) => part[0].toUpperCase() + part.slice(1))
    .join("");

/** "Kitchen Helper!" -> "kitchen_helper"; the brain validates the same shape. @param {string} name */
function slug(name) {
  const id = name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return /^[0-9]/.test(id) ? `agent_${id}` : id;
}

/** The file as the person would look for it: from the checkout root down. @param {string} path */
const shortPath = (path) => {
  const root = path.lastIndexOf("/innate-os/");
  return root === -1 ? path.split("/").slice(-3).join("/") : path.slice(root + 1);
};

/** @param {string[]} a @param {string[]} b */
const sameList = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);

/** @param {Storage} store @param {string} key */
function read(store, key) {
  try {
    return store.getItem(key);
  } catch {
    return null; // private mode: the story still runs, it just forgets between visits
  }
}

/** @param {Storage} store @param {string} key @param {string} value */
function write(store, key, value) {
  try {
    store.setItem(key, value);
  } catch {
    /* see read() */
  }
}

/** @typedef {import("../teleop/agentState.js").AgentEntry} AgentEntry */
/** @typedef {{ id: string, group: string, load_error: string }} SkillRow */
/** What the form holds; `isNew` until the brain has the file. */
/** @typedef {{ id: string, name: string, prompt: string, skills: string[], listen: boolean, gaze: boolean, isNew: boolean }} Draft */

/**
 * @param {HTMLElement} root the agent cockpit
 * @param {ReturnType<import("../teleop/agentState.js").sharedAgentState>} agentState
 * @param {any} session sim session (onChallenge/onEnvironment/startChallenge), or null on hardware
 * @param {any} panel the chat panel: offers, narration, onboarding
 * @param {{
 *   lastLine: () => string, spokenCount: () => number, cancelSkill: () => Promise<unknown>,
 *   motionAt: () => number, resetMotion: () => void, navigating?: () => boolean,
 *   recalledAt: () => number, turnedAt: () => number, armedAgent?: () => string,
 *   armAgent?: (id: string) => void, onCreateAgent?: (cb: () => void) => void,
 *   dockDirectives?: (host: HTMLElement | null) => void,
 *   dockStartStop?: (host: HTMLElement | null) => void,
 *   skillRoster?: (cb: (rows: any[]) => void) => () => void,
 *   directivesEl?: HTMLElement, showView?: (id: string) => void, revealCameras?: () => void,
 *   overlay: (cb: (event: any) => void) => () => void,
 * }} opts
 */
export function createAgentStudio(root, agentState, session, panel, opts) {
  const dock = document.createElement("div");
  dock.className = "agent-studio-dock open";

  const head = document.createElement("div");
  head.className = "agent-studio-dock-head";
  const headAction = document.createElement("span");
  headAction.className = "agent-studio-dock-action";

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "agent-studio-dock-toggle";
  // The rail's own Agent mark: a four-point sparkle for the autonomous brain.
  toggle.innerHTML =
    '<svg class="agent-studio-dock-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3.5l1.7 6.8 6.8 1.7-6.8 1.7L12 20.5l-1.7-6.8L3.5 12l6.8-1.7z"/></svg>' +
    '<span class="microlabel">Agent</span><strong class="agent-studio-title"></strong>' +
    '<span class="agent-studio-dock-chevron"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="6,9 12,15 18,9"/></svg></span>';
  const title = /** @type {HTMLElement} */ (toggle.querySelector(".agent-studio-title"));

  const panelEl = document.createElement("section");
  panelEl.className = "agent-studio";
  panelEl.id = "agent-studio-panel";
  panelEl.setAttribute("aria-label", "Agent detail");
  toggle.setAttribute("aria-controls", panelEl.id);

  const persona = document.createElement("span");
  persona.className = "agent-studio-persona";
  const note = document.createElement("p");
  note.className = "agent-studio-note";
  // Where the file lives, for whoever wants to edit it in code.
  const caption = document.createElement("p");
  caption.className = "agent-studio-caption mono";

  // The agent's display name. Renaming keeps the file it already has, so the id stays put.
  const nameField = document.createElement("label");
  nameField.className = "agent-studio-field";
  nameField.innerHTML = '<span class="microlabel">Name</span><input type="text" maxlength="60" placeholder="Kitchen helper">';
  const newNameInput = /** @type {HTMLInputElement} */ (nameField.querySelector("input"));

  // The personality half of an agent, in the visitor's words (the story's persona prompt).
  const promptRow = document.createElement("form");
  promptRow.className = "agent-studio-prompt";
  promptRow.innerHTML =
    '<span class="microlabel">Prompt</span>' +
    '<textarea rows="2" maxlength="240" aria-label="Robot personality prompt" placeholder="Who is this robot? e.g. a butler who has seen better days"></textarea>' +
    '<button type="submit">Make it so</button>';
  const promptInput = /** @type {HTMLTextAreaElement} */ (promptRow.querySelector("textarea"));

  // The agent's prompt as its file holds it; editable when the file is the form's own.
  const promptField = document.createElement("label");
  promptField.className = "agent-studio-field";
  promptField.innerHTML =
    '<span class="microlabel">Prompt</span>' +
    '<textarea rows="5" aria-label="Agent prompt" placeholder="You are MARS, a friendly robot assistant…"></textarea>';
  const promptText = /** @type {HTMLTextAreaElement} */ (promptField.querySelector("textarea"));

  const tabsRow = document.createElement("div");
  tabsRow.className = "agent-studio-tabs";
  tabsRow.setAttribute("role", "tablist");
  /** @type {Record<string, HTMLElement>} */ const panes = {};
  /** @type {Record<string, HTMLButtonElement>} */ const tabs = {};
  for (const [id, label] of [["identity", "Identity"], ["skills", "Skills"], ["advanced", "Advanced"]]) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "agent-studio-tab";
    button.setAttribute("role", "tab");
    button.textContent = label;
    button.addEventListener("click", () => {
      tab = id;
      chooserOpen = false;
      render(true);
    });
    tabsRow.append(button);
    tabs[id] = button;
    const pane = document.createElement("div");
    pane.className = "agent-studio-pane";
    panes[id] = pane;
  }

  const skills = document.createElement("ul");
  skills.className = "agent-studio-skills";

  // "Add skill": every skill the brain can run that the agent does not have yet.
  const addRow = document.createElement("div");
  addRow.className = "agent-studio-add";
  const addBtn = document.createElement("button");
  addBtn.type = "button";
  addBtn.className = "agent-studio-action quiet";
  addBtn.textContent = "Add skill";
  addBtn.setAttribute("aria-haspopup", "listbox");
  const chooser = document.createElement("div");
  chooser.className = "agent-studio-chooser";
  chooser.hidden = true;
  chooser.innerHTML =
    '<input type="search" class="agent-studio-chooser-search" placeholder="Find a skill" aria-label="Find a skill">' +
    '<ul class="agent-studio-chooser-list" role="listbox" aria-label="Skills to add"></ul>';
  const chooserSearch = /** @type {HTMLInputElement} */ (chooser.querySelector("input"));
  const chooserList = /** @type {HTMLElement} */ (chooser.querySelector("ul"));
  addRow.append(addBtn, chooser);

  const saveBar = document.createElement("div");
  saveBar.className = "agent-studio-savebar";
  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.className = "agent-studio-action";
  const discardBtn = document.createElement("button");
  discardBtn.type = "button";
  discardBtn.className = "agent-studio-action quiet";
  const status = document.createElement("span");
  status.className = "agent-studio-status";
  status.setAttribute("role", "status");
  saveBar.append(saveBtn, discardBtn, status);

  // Microphone and gaze: an agent's inputs, out of the way of who it is and what it can do.
  const checks = document.createElement("div");
  checks.innerHTML =
    '<label class="agent-studio-check"><input type="checkbox" data-field="listen"><span>Listens to the microphone</span></label>' +
    '<label class="agent-studio-check"><input type="checkbox" data-field="gaze"><span>Looks at people while talking</span></label>';
  const listenInput = /** @type {HTMLInputElement} */ (checks.querySelector('[data-field="listen"]'));
  const gazeInput = /** @type {HTMLInputElement} */ (checks.querySelector('[data-field="gaze"]'));
  const deleteBtn = document.createElement("button");
  deleteBtn.type = "button";
  deleteBtn.className = "agent-studio-action quiet danger";
  deleteBtn.textContent = "Delete agent";
  deleteBtn.addEventListener("click", () => void remove());

  const actions = document.createElement("div");
  actions.className = "agent-studio-actions";

  // The story's asks live here, where the agent's identity and skills live for every agent:
  // characters beside the prompt, a skill to grant above the skills it has. Below 820px
  // the dock stands down and the chat's own deck takes them.
  const deck = createOfferDeck();
  let deckKey = "";

  panes.identity.append(deck.el, promptRow, nameField, promptField);
  panes.skills.append(skills, addRow);
  panes.advanced.append(checks, caption, deleteBtn);
  panelEl.append(persona, note, tabsRow, panes.identity, panes.skills, panes.advanced, saveBar, actions);
  head.append(toggle, headAction);
  dock.append(head, panelEl);
  root.append(dock);

  // Bottom left of the stage, where the scene setup sits once the story is over. On the
  // stage and not the cockpit: the cockpit runs past the viewer's rounded edge.
  const leaveBtn = document.createElement("button");
  leaveBtn.type = "button";
  leaveBtn.className = "agent-skip-ahead";
  leaveBtn.hidden = true;
  (root.querySelector(".video-stage") ?? root).append(leaveBtn);

  // The door: the room goes white while the next world compiles behind it.
  const whiteout = document.createElement("div");
  whiteout.className = "agent-whiteout";
  whiteout.setAttribute("aria-hidden", "true");
  whiteout.innerHTML =
    '<div class="agent-whiteout-load"><span class="agent-whiteout-mark">Through the door</span>' +
    '<span class="agent-whiteout-bar"><span></span></span></div>';
  root.append(whiteout);

  /** Resolves on the first world frame, which is when the story is known either way. */
  /** @type {(() => void) | null} */ let settled = null;
  const knownFromTheWorld = new Promise((resolve) => {
    settled = () => {
      settled = null;
      resolve(undefined);
    };
  });
  // The 3D view builds after that, behind a scrim of its own: coming out from under the
  // splash into a page whose middle is still loading is the same flash, smaller.
  const stageReady = new Promise((resolve) =>
    document.addEventListener("innate:stage-ready", () => resolve(undefined), { once: true }),
  );

  /** @type {any} */ let challenge = null;
  /** @type {any} */ let environment = null;
  let dockOpen = false; // the stage comes first; the story and "Create agent" open it
  let wasInStory = false;
  let compact = false;
  let autoStarted = false;
  let armedAttempt = read(sessionStorage, ARMED_KEY) ?? "";
  let arrivalAttempt = "";
  let graduatedAttempt = "";
  let doorAttempt = "";
  let graduationReady = false;
  /** @type {ReturnType<typeof setInterval> | null} */ let graduationPoll = null;
  let seenAct = -1;
  let actSpoke = 0; // robot lines when the current act began; chips wait for one more
  let actAt = 0; // when it began, for the grant's patience
  let recallShown = 0;
  let turnShown = 0;
  let whiteUntil = 0;
  let lastChoice = { text: "", at: 0 };
  let playIntroPending = read(sessionStorage, PLAY_INTRO_KEY) === "1";
  if (playIntroPending) write(sessionStorage, PLAY_INTRO_KEY, "");
  // The form: unsaved edits to the selected agent, or a new agent altogether.
  /** @type {Draft | null} */ let draft = null;
  let saving = false;
  let saveStatus = "";
  let chooserOpen = false;
  let tab = "identity";
  /** The ask the tab last followed, so a person who switches tabs is not pulled back each frame. */
  let followedAsk = "";
  /** @type {SkillRow[]} */ let roster = [];
  /** @type {{ skill: string, at: number } | null} */ let landed = null;

  const active = () => challenge?.active ?? null;
  const story = () => (active()?.runtime?.story ? active() : null);
  const runtime = () => story()?.runtime ?? null;
  const out = () => (active()?.id === "way_out" ? active() : null);
  const passed = () => out()?.state === "passed";
  /** The story owns the interface exactly while the world is running it. Passing the last
   * challenge ends that: the rail, the scene setup and the challenges are the reward. */
  const storyRunning = () => !!story() || (!!out() && !passed());
  const switching = () => environment?.switch?.state === "loading";
  // A switch the world refused: say so, once, or Play the intro looks like it did nothing.
  let failedSwitch = "";
  function reportFailedSwitch() {
    const pending = environment?.switch;
    const key = pending?.state === "failed" ? `${pending.id}:${pending.message}` : "";
    if (key === failedSwitch) return;
    failedSwitch = key;
    if (!key) return;
    void panel.narrate(
      `The world could not switch to ${pending.display_name || pending.id}: ${pending.message}. ` +
        "The simulator's navigation stack is usually stuck when this happens; restart it with ./innate-sim down && ./innate-sim up --intro.",
      { local: true },
    );
  }
  const envId = () => environment?.environment?.id ?? "";
  const skipped = () => read(localStorage, SKIP_KEY) === "1";
  const spoken = () => opts.spokenCount();
  /** Persona and name: from the running story, else what the world carried over. */
  const profile = () => {
    const r = runtime();
    const carried = challenge?.profile ?? {};
    const own = r?.profile ?? {};
    return { persona: own.persona || carried.persona || "", name: own.name || carried.name || "" };
  };
  const storyAgent = () => agentState.get().agents.find((a) => a.id === STORY_AGENT) ?? null;
  /** The agent the chat panel shows: currentDirective is empty until Start, and a picked agent is still picked. */
  const currentAgent = () => {
    const s = agentState.get();
    const id = s.currentDirective || opts.armedAgent?.() || "";
    return s.agents.find((a) => a.id === id) ?? null;
  };
  const earnedSkills = () => (storyAgent()?.skills ?? []).filter((id) => !UNMENTIONED_SKILLS.has(id));
  /** Whether a robot line names a skill, however it punctuates it. @param {string | undefined} text @param {string} skill */
  const mentions = (text, skill) =>
    (text ?? "").toLowerCase().replace(/[^a-z]/g, "").includes(skillLabel(skill).toLowerCase());

  // A visit that lands in Nowhere with nothing running is a first run: begin it.
  function autoStart() {
    if (autoStarted || !session || skipped() || envId() !== "void" || !challenge || challenge.active) return;
    if (!challenge.list?.some((/** @type {any} */ c) => c.id === "nowhere")) return;
    autoStarted = true;
    session.startChallenge("nowhere");
  }

  // The story's agent arms itself; nobody should have to find a switch. A just-started
  // attempt arms fresh (resetting chat, memory and skills); one already under way is
  // resumed as it stands, so a reload changes nothing.
  function autoArm() {
    const s = story();
    if (!s || armedAttempt === s.attempt_id || !storyAgent()) return;
    armedAttempt = s.attempt_id;
    write(sessionStorage, ARMED_KEY, armedAttempt);
    const elapsedMs = Math.max(0, Number(s.elapsed_s) || 0) * 1000;
    const fresh = elapsedMs < 8000 || agentState.get().currentDirective !== STORY_AGENT;
    if (fresh) void agentState.setDirective(STORY_AGENT);
    panel.beginOnboarding(fresh, Date.now() - elapsedMs);
    seenAct = -1;
    actSpoke = spoken();
    actAt = Date.now();
    turnShown = 0;
    hideDragHint();
    opts.resetMotion();
    setCamera({ mode: "free", side: 0, back: null });
  }

  // The Backrooms feel familiar: the world says so, the robot asks for SearchMemory,
  // and the person grants it like every other skill.
  function arriveInBackrooms() {
    const o = out();
    if (!o || switching() || envId() !== "backrooms" || arrivalAttempt === o.attempt_id) return;
    arrivalAttempt = o.attempt_id;
    actSpoke = spoken();
    actAt = Date.now();
    setTimeout(() => void panel.narrate("Somewhere else. Yellow, this time.", { local: true }), 2500);
  }

  function restartIntro() {
    write(localStorage, SKIP_KEY, "");
    write(localStorage, DRAG_HINT_KEY, ""); // asking for the story again means asking for all of it
    write(sessionStorage, ARMED_KEY, "");
    armedAttempt = "";
    autoStarted = false;
    graduationReady = false;
    sceneTaught = false;
    panel.setOffers([]);
    leaveStage();
    session?.abortChallenge?.();
    if (envId() !== "void") session?.switchEnvironment?.("void");
    render(true);
  }

  // Skipping ends the story outright: the Backrooms with an ordinary agent, no next
  // challenge to play. Aborting is what ends story mode, so the rail, the scene setup
  // and the challenges come straight back.
  function leaveStory() {
    write(localStorage, SKIP_KEY, "1");
    panel.setOffers([]);
    leaveStage();
    session?.abortChallenge?.();
    if (envId() !== "backrooms") session?.switchEnvironment?.("backrooms");
    void agentState.setDirective(SKIPPED_AGENT);
    render(true);
  }

  // Finding the way out is done on the lidar: while the robot drives the Backrooms, the walls
  // show as it senses them. The scene setup's own Lidar chip is what gets pressed, so the
  // interface agrees with the stage and the person sees where the switch lives.
  let lidarShown = false;
  let lidarExplained = false;
  /** @param {boolean} on */
  function showLidar(on) {
    if (on === lidarShown) return;
    const chip = [...root.querySelectorAll(".sim-scene-setup .sim-scene-button")].find((b) => b.textContent?.trim() === "Lidar");
    if (!(chip instanceof HTMLElement)) return;
    lidarShown = on;
    if ((chip.getAttribute("aria-pressed") === "true") !== on) chip.click();
    if (!on || lidarExplained) return;
    lidarExplained = true;
    void panel.narrate("The dots are its lidar: the walls as MARS senses them.", { local: true });
  }

  /** Whatever the last run left mid-air: a running skill, a cued tile, and a camera the
   * story switched to for a grasp. None of it belongs to the next thing the person does. */
  function leaveStage() {
    void opts.cancelSkill().catch(() => {});
    uncue();
    showLidar(false);
    hideDragHint();
    opts.showView?.("orbit");
  }

  // The ending points at the scene setup, bottom left, where the next world is chosen: the
  // toggle until it opens, then the environment picker until it is used. Learned once, it
  // stays learned.
  /** @type {(() => void) | null} */ let sceneCue = null;
  /** @type {HTMLElement | null} */ let sceneCueTarget = null;
  let sceneTaught = false;
  const sceneSetup = () => root.querySelector(".sim-scene-setup");
  const sceneSetupOpen = () => !!sceneSetup()?.classList.contains("open");
  /** @param {boolean} on */
  function inviteSceneSetup(on) {
    const setup = sceneSetup();
    const target = !on || !setup ? null : setup.querySelector(sceneSetupOpen() ? ".sim-environment-section:not([hidden])" : ".sim-scene-toggle");
    if (target === sceneCueTarget) return;
    sceneCue?.();
    sceneCue = null;
    sceneCueTarget = target instanceof HTMLElement ? target : null;
    if (sceneCueTarget) sceneCue = cue(sceneCueTarget, sceneSetupOpen() ? "Pick a world" : "Change the world");
  }

  // The narration waits for the robot's closing line, so the ending is not talked over.
  /** @param {any} o */
  function armGraduation(o) {
    graduatedAttempt = o.attempt_id;
    graduationReady = false;
    sceneTaught = false;
    void opts.cancelSkill().catch(() => {});
    setTimeout(() => void panel.narrate("You're out. You made it.", { local: true }), 600);
    const before = spoken();
    const deadline = Date.now() + GRADUATION_MAX_WAIT_MS;
    if (graduationPoll) clearInterval(graduationPoll);
    graduationPoll = setInterval(() => {
      if (!((spoken() > before && !panel.isBusy()) || Date.now() > deadline)) return;
      if (graduationPoll) clearInterval(graduationPoll);
      graduationPoll = null;
      graduationReady = true;
      agentState.setActiveSkills(earnedSkills(), STORY_AGENT);
      const { name } = profile();
      panel.addNotice(`You built this agent${name ? `: ${name}` : ""}. Its skills are all yours now.`);
      void panel.narrate(
        "The rest of the interface is yours too. Scene setup, bottom left, is where you pick the next world: the apartment, or the crossroads. The challenges sit beside it, and the left rail has Teleop, the map and the settings.",
        { local: true },
      );
      render(true);
    }, 500);
  }

  /** @param {string} skill @param {boolean} [announce] */
  async function grant(skill, announce = true) {
    const next = new Set(agentState.get().activeSkills);
    next.add(skill);
    agentState.setActiveSkills([...next], STORY_AGENT);
    if (!announce) return;
    // The skill and the line telling it to use the skill travel separately, and the brain
    // can take its turn on the line before the toolset has caught up -- which reads to the
    // robot as the skill failing. Say it once the tool is actually there.
    await held(() => agentState.get().activeSkills.has(skill), GRANT_WAIT_MS);
    landed = { skill, at: Date.now() };
    render(true);
    setTimeout(() => render(true), LANDED_MS);
    void panel.submitText(GRANT_LINES[skill] ?? `Granted: the ${skillLabel(skill)} skill.`);
  }

  /** @param {string} skill */
  const grantOffer = (skill) => ({
    text: skillLabel(skill),
    kind: /** @type {const} */ ("grant"),
    ...skillCard(skill),
    onSelect: () => void grant(skill),
  });

  /** @param {string} persona */
  function choose(persona) {
    if (persona === lastChoice.text && Date.now() - lastChoice.at < 5000) return;
    lastChoice = { text: persona, at: Date.now() };
    session?.sendChallengeEvent?.({ type: "persona", persona });
    // The choice travels to the world and comes back as the act advancing. When it does not,
    // the story sits on a screen whose only control has already been used, with nothing said
    // anywhere: leave the trail in the console, which is all a deployed session has.
    void held(() => !!profile().persona || !storyRunning(), PERSONA_ECHO_MS).then(() => {
      if (profile().persona || !storyRunning()) return;
      console.warn("[story] the world never took the persona:", persona);
    });
    // An offered character is someone the robot becomes; typed words are a prompt it is handed.
    const offered = (runtime()?.personas ?? []).includes(persona);
    void panel.submitText(offered ? `From now on, you are ${persona}.` : `From now on, your prompt is: ${persona}`);
  }

  /** Why the chip row shows what it shows; readable in DevTools as data-chips on the panel. */
  let chipReason = "";
  /** The robot asks first: a grant offered before the request spoils the turn-taking.
   *  It may also ask in the turn that ends the previous act, hence the second test.
   *  @param {string[]} wants */
  const asked = (wants) =>
    spoken() > actSpoke ||
    Date.now() - actAt > GRANT_GRACE_MS ||
    wants.some((skill) => mentions(opts.lastLine(), skill));

  /** @returns {{ chips: import("./offerDeck.js").Offer[], title?: string }} */
  function offers() {
    const r = runtime();
    const o = out();
    const name = profile().name || "MARS";
    if (switching()) {
      chipReason = "frozen";
      return { chips: [] }; // nothing lands while the world changes
    }
    if (o?.state === "passed") {
      chipReason = graduationReady ? "graduated" : "graduating"; // the next world is chosen in the scene setup
      return { chips: [] };
    }
    if (o) {
      if (agentState.get().activeSkills.has(MEMORY)) {
        chipReason = "memory-granted";
        return { chips: [] };
      }
      const ready = asked([MEMORY]);
      chipReason = ready ? "grants:memory" : "waiting-for-line";
      return { title: `${name} asks for a skill`, chips: ready ? [grantOffer(MEMORY)] : [] };
    }
    if (!r) {
      chipReason = "no-story";
      return { chips: [] };
    }
    const personas = r.personas ?? [];
    if (personas.length) {
      // The persona choice owns the deck: nothing else competes with it.
      if (!asked([])) {
        chipReason = "waiting-for-line";
        return { chips: [] };
      }
      chipReason = `personas:${personas.length}`;
      const pick = (/** @type {string} */ p) => ({
        text: p,
        kind: /** @type {const} */ ("persona"),
        ...personaCard(p),
        onSelect: () => choose(p),
      });
      return {
        title: `Who is ${name}?`,
        chips: [
          ...personas.map(pick),
          { text: "Surprise me", kind: "random", onSelect: () => choose(personas[Math.floor(Math.random() * personas.length)]) },
          // The panel has the prompt right there; only the chat's deck needs a way to the composer.
          ...(compact ? [{ text: "Write your own", kind: /** @type {const} */ ("custom"), onSelect: () => panel.focusComposer() }] : []),
        ],
      };
    }
    const wants = (r.wants ?? []).filter(
      (/** @type {string} */ skill) => !agentState.get().activeSkills.has(skill) && skill !== WAVE,
    );
    // What the act says the person might say next: the story's own words, not a tool call.
    const replies = (r.suggests ?? []).map((/** @type {string} */ text) => ({
      text,
      kind: /** @type {const} */ ("reply"),
      onSelect: (/** @type {string} */ said) => void panel.submitText(said),
    }));
    if (!wants.length || !asked(wants)) {
      chipReason = wants.length ? "waiting-for-line" : "granted";
      return { chips: replies };
    }
    chipReason = `grants:${wants.join(",")}`;
    return { title: `${name} asks for a skill`, chips: [...wants.map(grantOffer), ...replies] };
  }

  let camera = { mode: /** @type {"free" | "chase"} */ ("free"), side: 0, back: /** @type {number | null} */ (null), height: /** @type {number | null} */ (null) };
  /** @param {{mode: "free" | "chase", side: number, back: number | null, height?: number | null}} want back < 0 faces the robot from the front */
  function setCamera(want) {
    camera = { ...want, height: want.height ?? null };
    document.dispatchEvent(
      new CustomEvent("innate:camera-mode", {
        detail: {
          mode: want.mode,
          side: want.side,
          ...(want.back == null ? {} : { back: want.back }),
          ...(camera.height == null ? {} : { height: camera.height }),
        },
      }),
    );
  }
  /** The story opens on the page's own framing, the robot seen from the front. @param {string} label */
  function cameraFor(label) {
    // Close, high and a little to the right: the robot's back fills the frame, the cube sits ahead of it.
    if (label === "Pick up the cube") return { mode: /** @type {const} */ ("chase"), side: 0.35, back: 0.8, height: 1.3 };
    // The door stands 3 m out: look over the robot's back at it.
    if (label === "Go through the door") return { mode: /** @type {const} */ ("chase"), side: 0, back: 1.4, height: 1.2 };
    // The point of this act is watching the robot turn, which a chase camera would hide by
    // turning with it: hold the view still.
    if (label === "Look around") return { mode: /** @type {const} */ ("free"), side: 0, back: null };
    // From the persona act on, face the robot for the conversation; fall in behind it once it drives.
    // Until it drives, the stage's own framing stands: the same close view of the robot
    // the Agent page opens with, and a drag of it sticks.
    if (!opts.motionAt()) return { mode: /** @type {const} */ ("free"), side: 0, back: null };
    return { mode: /** @type {const} */ ("chase"), side: 0, back: null };
  }

  // The pickup is one uninterruptible action, so MARS cannot narrate it; the interface
  // points at the camera that shows each phase and says so.
  /** @type {(() => void) | null} */ let uncueTile = null;
  function uncue() {
    uncueTile?.();
    uncueTile = null;
  }
  /** @param {string} view @param {string} line */
  function cueCamera(view, line) {
    uncue();
    opts.revealCameras?.(); // on a phone the tiles sit behind a toggle; open it before pointing
    const tile = [...root.querySelectorAll(".cam-tile")].find(
      (t) => t.querySelector(".cam-tile-label")?.textContent?.trim().toLowerCase() === view,
    );
    if (!(tile instanceof HTMLElement)) return; // already the big view, or not offered
    uncueTile = cue(tile, "click here");
    requestAnimationFrame(() => closeIn(tile)); // after the strip has opened and the tile has its place
    void panel.narrate(line, { local: true });
  }

  // The first turn is also the visitor's first chance to learn the 3D view moves.
  /** @type {HTMLElement | null} */ let dragHint = null;
  function hideDragHint() {
    dragHint?.remove();
    dragHint = null;
  }
  function showDragHint() {
    if (read(localStorage, DRAG_HINT_KEY) === "1") return;
    write(localStorage, DRAG_HINT_KEY, "1");
    dragHint = document.createElement("div");
    dragHint.className = "agent-drag-hint";
    dragHint.textContent = "drag to look around";
    // On the stage, not the cockpit: the cockpit's width includes the chat, which would
    // push a "centred" hint off to the right of the 3D view.
    (root.querySelector(".video-stage") ?? root).append(dragHint);
    root.querySelector("canvas")?.addEventListener("pointerdown", hideDragHint, { once: true });
    setTimeout(hideDragHint, 15_000);
    void panel.narrate("MARS is turning to look around. Drag the view with your mouse to look around too.", {
      local: true,
    });
  }

  /** @param {AgentEntry} agent */
  const draftOf = (agent) => ({
    id: agent.id,
    name: agent.name,
    prompt: agent.prompt,
    skills: [...agent.skills],
    listen: agent.listen,
    gaze: agent.gaze,
    isNew: false,
  });
  /** The form's values: the draft while one is open, else the agent as the brain has it. */
  const form = () => draft ?? (currentAgent() ? draftOf(/** @type {AgentEntry} */ (currentAgent())) : null);
  /** @param {AgentEntry | null} agent */
  const editable = (agent) => !!draft?.isNew || (!!agent && agent.source === "user" && agent.editable);
  /** @param {AgentEntry | null} agent */
  const dirty = (agent) =>
    !!draft &&
    (draft.isNew ||
      !agent ||
      draft.name !== agent.name ||
      draft.prompt !== agent.prompt ||
      draft.listen !== agent.listen ||
      draft.gaze !== agent.gaze ||
      !sameList(draft.skills, agent.skills));

  /** Start editing the selected agent, keeping edits already made. */
  function edit() {
    const agent = currentAgent();
    if (!draft && agent) draft = draftOf(agent);
    return draft;
  }

  function createAgent() {
    draft = { id: "", name: "", prompt: "", skills: [], listen: true, gaze: true, isNew: true };
    tab = "identity";
    saveStatus = "";
    chooserOpen = false;
    setDockOpen(true);
    requestAnimationFrame(() => newNameInput.focus());
  }

  function discard() {
    draft = null;
    saveStatus = "";
    chooserOpen = false;
    render(true);
  }

  /** A brain built before the agent services answers every call with the same rosbridge error. */
  const failure = (/** @type {string} */ verb, /** @type {unknown} */ err) => {
    const message = err instanceof Error ? err.message : String(err);
    if (/service/i.test(message)) return `${verb} failed: this brain has no agent services yet. Rebuild it and start again.`;
    return `${verb} failed: ${message}`;
  };

  async function save() {
    const d = draft;
    if (!d || saving) return;
    const id = d.isNew ? slug(d.name) : d.id;
    if (!id) {
      saveStatus = "Give it a name first.";
      render(true);
      newNameInput.focus();
      return;
    }
    saving = true;
    saveStatus = "Saving…";
    render(true);
    try {
      const res = await agentState.saveAgent({
        id,
        display_name: d.name.trim(),
        prompt: d.prompt,
        skill_ids: d.skills,
        listen: d.listen,
        gaze: d.gaze,
      });
      if (!res.success) {
        saveStatus = res.message || "Save failed.";
        return;
      }
      draft = null;
      chooserOpen = false;
      saveStatus = res.message ? `Saved, but it did not load: ${res.message}` : "";
      if (d.isNew) opts.armAgent?.(id);
    } catch (err) {
      saveStatus = failure("Save", err);
    } finally {
      saving = false;
      render(true);
    }
  }

  async function remove() {
    const agent = currentAgent();
    if (!agent || saving) return;
    if (!window.confirm(`Delete "${agent.name}"? This removes ${shortPath(agent.path) || "its file"}.`)) return;
    saving = true;
    saveStatus = "Deleting…";
    render(true);
    try {
      const res = await agentState.deleteAgent(agent.id);
      saveStatus = res.success ? "" : res.message || "Delete failed.";
      if (res.success) draft = null;
    } catch (err) {
      saveStatus = failure("Delete", err);
    } finally {
      saving = false;
      render(true);
    }
  }

  /** @param {string} id */
  function addSkill(id) {
    const d = edit();
    if (!d || d.skills.includes(id)) return;
    d.skills.push(id);
    render(true);
  }

  /** @param {string} id */
  function removeSkill(id) {
    const d = edit();
    if (!d) return;
    d.skills = d.skills.filter((s) => s !== id);
    render(true);
  }

  /** @param {boolean} open */
  function applyDockOpen(open) {
    dockOpen = open;
    dock.classList.toggle("open", open);
    toggle.setAttribute("aria-expanded", String(open));
    toggle.setAttribute("aria-label", open ? "Close agent detail" : "Open agent detail");
  }

  /** @param {boolean} open */
  function setDockOpen(open) {
    applyDockOpen(open);
    render(true);
  }

  let renderedKey = "";
  /** @param {boolean} [force] */
  function render(force = false) {
    const s = agentState.get();
    const agent = currentAgent();
    const r = runtime();
    const o = out();
    const { persona: who, name } = profile();
    const env = envId();
    // World frames arrive at physics rate; touch the DOM only when something shown here changed.
    const key = JSON.stringify([
      s.currentDirective, [...s.activeSkills].sort(), agent, r && { ...r, brief: undefined },
      o && { id: o.id, state: o.state, attempt_id: o.attempt_id }, who, name, env, switching(), dockOpen,
      graduationReady, sceneTaught, sceneSetupOpen(), spoken(), Date.now() < whiteUntil, opts.motionAt() > 0, opts.navigating?.(),
      opts.recalledAt(), opts.turnedAt(),
      draft, saving, saveStatus, chooserOpen, tab, roster.length,
    ]);
    if (!force && key === renderedKey) return;
    renderedKey = key;

    // Act bookkeeping: the chip gate, the grasp camera, the world's opening line.
    const actChanged = !!r && r.act !== seenAct;
    if (actChanged) {
      // A resumed page must not wait for a line the robot said before the reload.
      actSpoke = seenAct >= 0 || (Number(story()?.elapsed_s) || 0) < 8 ? spoken() : -1;
      actAt = Date.now();
      const opener = ACT_OPENERS[r.label];
      if (opener) setTimeout(() => void panel.narrate(opener, { local: true }), 300);
      seenAct = r.act;
    }
    if (r) {
      // A new act always re-asserts its camera: the visitor may have dragged or switched away.
      const want = cameraFor(r.label);
      const same = want.mode === camera.mode && want.side === camera.side && want.back === camera.back && (want.height ?? null) === camera.height;
      if (actChanged || !same) setCamera(want);
    }

    // A navigation still running would drive to void coordinates in the next world.
    const doorPassed = (r?.finished || (switching() && env === "void")) && armedAttempt && doorAttempt !== armedAttempt;
    if (doorPassed) {
      doorAttempt = armedAttempt;
      void opts.cancelSkill().catch(() => {});
      setTimeout(() => void panel.narrate("Through the door.", { local: true }), 400);
    }
    if (r?.finished || (switching() && env === "void")) whiteUntil = Date.now() + 6000;
    if (o && env === "backrooms" && !switching()) whiteUntil = Math.min(whiteUntil, Date.now() + 800);
    const white = Date.now() < whiteUntil;
    whiteout.classList.toggle("on", white);
    if (white) setTimeout(() => render(), whiteUntil - Date.now() + 50);

    if (playIntroPending && environment) {
      playIntroPending = false;
      restartIntro();
    }
    // TurnInPlace draws no overlay, so the first turn arrives as a skill status.
    if (r && opts.turnedAt() > turnShown) {
      turnShown = opts.turnedAt();
      showDragHint();
    }
    // The memory beat has to be seen, not only heard: point the visitor at the map.
    if (o && env === "backrooms" && opts.recalledAt() > recallShown) {
      recallShown = opts.recalledAt();
      setTimeout(() => {
        cueCamera("map", "Look at the map: that is the spot it remembered.");
        setTimeout(uncue, 12_000);
      }, 1500);
    }
    if (o?.state === "passed" && graduatedAttempt !== o.attempt_id) armGraduation(o);
    showLidar(!!o && o.state !== "passed" && env === "backrooms" && !switching() && !!opts.navigating?.());
    inviteSceneSetup(!!o && o.state === "passed" && graduationReady && env === "backrooms" && !switching() && !sceneTaught);

    // Story mode: the world is running the story, and only then.
    const inStory = storyRunning();
    const graduated = !!o && o.state === "passed" && graduationReady;
    // The reveal is the story's last page: keep the panel open through it.
    const owned = inStory || graduated;
    if (owned !== wasInStory) {
      wasInStory = owned;
      applyDockOpen(owned);
    }
    document.body.classList.toggle("story-active", inStory);
    // Nowhere has no map worth reading, and a robot that cannot move has nothing to plot on
    // one: the map tile and the path it draws arrive with the Backrooms, where the recalled
    // exit is a place on that map.
    root.classList.toggle("story-mapless", inStory && env === "void");
    root.classList.toggle("story-active", inStory);
    panelEl.classList.toggle("story", inStory);

    // Picking another agent drops edits to the previous one; a new agent survives the pick.
    if (draft && !draft.isNew && draft.id !== agent?.id) draft = null;
    const isNew = !!draft?.isNew && !inStory;
    const f = inStory ? null : form();
    const canEdit = !inStory && editable(agent);

    title.textContent = inStory
      ? name || "MARS"
      : isNew
        ? draft?.name.trim() || "New agent"
        : (draft?.name.trim() || agent?.name) ?? "No agent";
    panel.setDisplayName(owned ? name || "MARS" : null);
    persona.textContent = who;
    persona.hidden = !who || !owned;
    note.textContent = noteFor(r, o, graduated, agent, isNew);
    note.hidden = !note.textContent;
    note.classList.toggle("warn", !inStory && !!agent && !isNew && !canEdit);
    caption.textContent = isNew
      ? `innate-os/workspace/custom_agents/${slug(draft?.name ?? "") || "…"}.py`
      : shortPath(agent?.path ?? "");
    caption.hidden = !caption.textContent;
    // Which agent runs, and resetting its brain, are not part of the story: it arms its own.
    if (opts.directivesEl) opts.directivesEl.hidden = inStory || (isNew && !compact);

    // The story's own inputs.
    promptRow.hidden = !inStory;
    // Including back to empty: a restarted story is nobody yet, and last run's words are not its prompt.
    if (promptInput.dataset.shown !== who && document.activeElement !== promptInput) {
      promptInput.value = who;
      promptInput.dataset.shown = who;
    }

    // What the story asks for goes to the panel: characters beside the prompt, a grant above
    // the skills. The chat keeps the replies, and everything when there is no panel.
    const { chips, title: ask } = offers();
    panelEl.dataset.chips = chipReason;
    const toPanel = compact ? [] : chips.filter((c) => c.kind === "persona" || c.kind === "grant" || c.kind === "random");
    const askKey = toPanel.filter((c) => c.kind !== "random").map((c) => `${c.kind}:${c.text}`).join("|");
    if (askKey !== deckKey) {
      deckKey = askKey;
      deck.set(toPanel, ask);
      const host = toPanel[0]?.kind === "grant" ? panes.skills : panes.identity;
      if (deck.el.parentElement !== host) host.prepend(deck.el);
    }
    // The panel shows the half of the agent being asked about, once per ask.
    if (askKey && askKey !== followedAsk) {
      followedAsk = askKey;
      tab = toPanel[0].kind === "grant" ? "skills" : "identity";
    }
    panel.setOffers(chips.filter((c) => !toPanel.includes(c)), toPanel.length ? "" : ask);
    // One tab at a time, in the story too.
    tabsRow.hidden = !inStory && !f;
    tabs.advanced.hidden = inStory;
    for (const [id, pane] of Object.entries(panes)) {
      pane.hidden = id !== tab || (!inStory && !f);
      tabs[id].setAttribute("aria-selected", String(id === tab));
    }
    listenInput.checked = !!f?.listen;
    gazeInput.checked = !!f?.gaze;
    listenInput.disabled = gazeInput.disabled = !canEdit;
    deleteBtn.hidden = isNew || agent?.source !== "user";
    deleteBtn.disabled = saving;
    nameField.hidden = !f;
    newNameInput.readOnly = !canEdit;
    if (f && document.activeElement !== newNameInput && newNameInput.value !== f.name) newNameInput.value = f.name;
    promptField.hidden = inStory || !f;
    promptText.readOnly = !canEdit;
    promptText.placeholder = canEdit ? "You are MARS, a friendly robot assistant…" : "No prompt.";
    if (f && document.activeElement !== promptText && promptText.value !== f.prompt) promptText.value = f.prompt;
    renderSkills(inStory, r, agent, f, canEdit);
    addRow.hidden = !canEdit;
    chooser.hidden = !chooserOpen;
    addBtn.setAttribute("aria-expanded", String(chooserOpen));
    if (chooserOpen) renderChooser(f?.skills ?? []);
    const showSave = canEdit && (isNew || dirty(agent));
    saveBar.hidden = !(showSave || saveStatus);
    saveBtn.hidden = !showSave;
    discardBtn.hidden = !showSave;
    saveBtn.textContent = isNew ? "Create" : "Save";
    discardBtn.textContent = isNew ? "Cancel" : "Discard";
    saveBtn.disabled = saving;
    discardBtn.disabled = saving;
    status.textContent = saveStatus;
    status.classList.toggle("error", !!saveStatus && !saving);

    actions.replaceChildren();
    if (session && !switching()) {
      if (graduated) {
        const repo = document.createElement("a");
        repo.className = "agent-studio-action quiet agent-studio-link";
        repo.href = "https://github.com/innate-inc/innate-os";
        repo.target = "_blank";
        repo.rel = "noopener";
        repo.textContent = "Clone MARS on GitHub, teach it more";
        actions.append(repo);
      }
    }
    leaveBtn.hidden = !(inStory && !!session && !switching());
    leaveBtn.textContent = r ? "Skip intro" : "Leave the story";
    leaveBtn.title = "Out of the story and back to the rest of the interface";

    // Who the robot is, is decided by the world, not in chat with the robot: the answer is
    // made in the panel, or typed here where there is no panel and sent there as a persona.
    const choosing = inStory && (r?.personas ?? []).length > 0;
    panel.setComposerAsk(
      !choosing ? null
        : compact ? { placeholder: "Or describe your own character…", submit: choose }
        : { placeholder: `Pick who ${name || "MARS"} is, top left` },
    );
  }

  /** @param {any} r @param {any} o @param {boolean} graduated @param {AgentEntry | null} agent @param {boolean} isNew */
  function noteFor(r, o, graduated, agent, isNew) {
    if (r) return r.finished ? "Through the door." : "";
    if (graduated) {
      // Said in the chat too, but a history sync drops display-only lines; this stays.
      return "It found the way out. This is the agent you built. Scene setup and the challenges are at the bottom of the stage, and the rail on the left has Teleop, the map and the settings.";
    }
    if (o) return "Find the way out.";
    if (isNew) return "An agent is a prompt plus skills. Name it, tell it who it is, and add what it may do.";
    if (!agent) return "Pick an agent to see its prompt and skills.";
    if (agent.source === "shipped") return "This is an innate agent. Create your own agent, or edit this one in code.";
    return agent.editable ? "" : "This agent's file holds more than this form. Edit it in code.";
  }

  /**
   * @param {boolean} inStory @param {any} r @param {AgentEntry | null} agent
   * @param {Draft | null} f @param {boolean} canEdit
   */
  function renderSkills(inStory, r, agent, f, canEdit) {
    const s = agentState.get();
    // In the story the roster is the story's agent, and only what it has been offered:
    // unasked rows would spoil the arc.
    const listed = (inStory ? storyAgent()?.skills : f?.skills) ?? [];
    const unlocked = new Set(r?.unlocked ?? []);
    const wanted = new Set(r?.wants ?? (inStory ? [MEMORY] : []));
    skills.replaceChildren();
    for (const id of listed) {
      if (r && !unlocked.has(id)) continue;
      if (inStory && !r && UNMENTIONED_SKILLS.has(id) && !s.activeSkills.has(id)) continue;
      const granted = s.activeSkills.has(id);
      if (inStory && wanted.has(id) && !granted) continue; // the grant card above is that skill
      const row = document.createElement("li");
      row.className = "agent-studio-skill";
      row.classList.toggle("granted", inStory && granted);
      const fresh = landed?.skill === id && Date.now() - landed.at < LANDED_MS;
      row.classList.toggle("landed", fresh);
      if (inStory) {
        const glyph = document.createElement("span");
        glyph.className = "agent-studio-skill-icon";
        glyph.innerHTML = skillCard(id).icon;
        row.append(glyph);
      }
      const label = document.createElement("span");
      label.className = "agent-studio-skill-name";
      label.textContent = skillLabel(id);
      label.title = id;
      row.append(label);
      if (inStory) {
        const state = document.createElement("span");
        state.className = "agent-studio-skill-state microlabel";
        state.textContent = fresh ? "new" : "granted";
        row.append(state);
      } else if (canEdit) {
        const del = document.createElement("button");
        del.type = "button";
        del.className = "agent-studio-skill-remove";
        del.setAttribute("aria-label", `Remove ${skillLabel(id)}`);
        del.textContent = "×";
        del.addEventListener("click", () => removeSkill(id));
        row.append(del);
      }
      skills.append(row);
    }
    if (!skills.childElementCount && (inStory || f)) {
      const empty = document.createElement("li");
      empty.className = "agent-studio-empty";
      empty.textContent = inStory ? "No skills yet. It will ask for them." : "No skills yet.";
      skills.append(empty);
    }
  }

  /** The skills the agent could still get, grouped as the brain groups them. @param {string[]} have */
  function renderChooser(have) {
    const q = chooserSearch.value.trim().toLowerCase();
    const has = new Set(have);
    const rows = roster
      .filter((sk) => !has.has(sk.id) && !sk.load_error)
      .filter((sk) => !q || `${skillLabel(sk.id)} ${sk.id} ${sk.group}`.toLowerCase().includes(q))
      .sort((a, b) => a.group.localeCompare(b.group) || skillLabel(a.id).localeCompare(skillLabel(b.id)));
    chooserList.replaceChildren();
    let group = null;
    for (const sk of rows) {
      if (sk.group !== group) {
        group = sk.group;
        const head = document.createElement("li");
        head.className = "agent-studio-chooser-group microlabel";
        head.textContent = group || "general";
        chooserList.append(head);
      }
      const item = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "agent-studio-chooser-option";
      btn.setAttribute("role", "option");
      btn.textContent = skillLabel(sk.id);
      btn.title = sk.id;
      btn.addEventListener("click", () => addSkill(sk.id));
      item.append(btn);
      chooserList.append(item);
    }
    if (!rows.length) {
      const empty = document.createElement("li");
      empty.className = "agent-studio-empty";
      empty.textContent = roster.length ? (q ? "Nothing matches." : "It has every skill there is.") : "Waiting for the skill roster…";
      chooserList.append(empty);
    }
  }

  toggle.addEventListener("click", () => setDockOpen(!dockOpen));
  applyDockOpen(false);
  // Narrow screens keep the bottom sheet they had: the picker goes back to the chat
  // panel and the editor stands down until there is room for it.
  const setCompact = (/** @type {boolean} */ on) => {
    compact = on;
    dock.hidden = on;
    opts.dockDirectives?.(on ? null : panelEl);
    // Compact leaves Start/Stop to the sheet's own header, which has already claimed it.
    if (!on) opts.dockStartStop?.(headAction);
    render(true);
  };
  setCompact(false);
  leaveBtn.addEventListener("click", leaveStory);
  promptRow.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = promptInput.value.trim();
    if (text) choose(text);
  });
  newNameInput.addEventListener("input", () => {
    const d = edit();
    if (d) d.name = newNameInput.value;
    render(true);
  });
  promptText.addEventListener("input", () => {
    if (promptText.readOnly) return;
    const d = edit();
    if (d) d.prompt = promptText.value;
    render(true);
  });
  addBtn.addEventListener("click", () => {
    chooserOpen = !chooserOpen;
    render(true);
    if (!chooserOpen) return;
    requestAnimationFrame(() => {
      chooserSearch.focus();
      chooser.scrollIntoView({ block: "nearest" });
    });
  });
  chooserSearch.addEventListener("input", () => render(true));
  for (const [input, field] of [[listenInput, "listen"], [gazeInput, "gaze"]]) {
    input.addEventListener("change", () => {
      const d = edit();
      if (d) d[field] = input.checked;
      render(true);
    });
  }
  chooser.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    chooserOpen = false;
    render(true);
    addBtn.focus();
  });
  // Capture-phase pointerdown: the stage and the picker swallow clicks, and a drag never
  // makes one. The row spans the panel, so only the button and the list itself count as inside.
  const onOutsideClick = (/** @type {PointerEvent} */ event) => {
    const path = event.composedPath();
    if (chooserOpen && !path.includes(addBtn) && !path.includes(chooser)) {
      chooserOpen = false;
      render(true);
    }
  };
  document.addEventListener("pointerdown", onOutsideClick, true);
  // Using the environment picker is the lesson; the world changing under it is the proof.
  const onSceneChange = (/** @type {Event} */ event) => {
    if (!(event.target instanceof Element) || !event.target.closest(".sim-environment-section")) return;
    sceneTaught = true;
    render(true);
  };
  root.addEventListener("change", onSceneChange, true);
  saveBtn.addEventListener("click", () => void save());
  discardBtn.addEventListener("click", discard);
  opts.onCreateAgent?.(createAgent);
  const unsubRoster = opts.skillRoster?.((rows) => {
    roster = rows
      .filter((sk) => sk && typeof sk.id === "string")
      .map((sk) => ({
        id: String(sk.id),
        group: typeof sk.group === "string" ? sk.group : "",
        load_error: typeof sk.load_error === "string" ? sk.load_error : "",
      }));
    render();
  });

  const unsubOverlay = opts.overlay((event) => {
    const skill = String(event?.skill ?? "").replace(/[^a-z0-9]/gi, "").toLowerCase();
    if (!story() || !skill.endsWith("pickanyobject")) return;
    if (event.ev === "run" && event.state === "start") {
      cueCamera("main", "Watch the approach on the MAIN camera. You can keep talking to MARS while it works.");
    } else if (event.ev === "stage" && event.name === "align") {
      cueCamera("arm", "Now the ARM camera, for the grasp.");
    } else if (event.ev === "run" && event.state === "end") {
      uncue();
      opts.showView?.("orbit"); // the grasp was watched on a camera; the story continues in the scene
    }
  });
  const unsubAgent = agentState.subscribe(() => render());
  const unsubChallenge = session?.onChallenge?.((/** @type {any} */ block) => {
    challenge = block;
    settled?.();
    autoStart();
    autoArm();
    arriveInBackrooms();
    render();
  });
  const unsubEnvironment = session?.onEnvironment?.((/** @type {any} */ roster) => {
    environment = roster;
    reportFailedSwitch();
    autoStart();
    arriveInBackrooms();
    render();
  });
  // The robot's lines arrive as chat, not world frames: re-check the chip gate on a pulse.
  const spokenPoll = setInterval(() => render(), 1000);
  // The stage frames the robot in free orbit whenever it (re)attaches, which can land after
  // this page's first request: forget what was asked and ask again.
  const onCameraReset = () => {
    camera = { mode: "free", side: 0, back: null, height: null };
    render(true);
  };
  document.addEventListener("innate:camera-reset", onCameraReset);
  // The rail's "Play the intro": an event for an open page, the flag for one still mounting.
  const onPlayIntro = () => {
    playIntroPending = false;
    write(sessionStorage, PLAY_INTRO_KEY, "");
    restartIntro();
  };
  document.addEventListener("innate:play-intro", onPlayIntro);
  render();

  return {
    setCompact,
    /** Resolves once the world has said whether a story is running, so the boot splash can
     * cover the moment rather than the interface appearing and half of it leaving. */
    settled: () => (session ? Promise.all([knownFromTheWorld, stageReady]) : Promise.resolve()),
    destroy() {
      unsubOverlay();
      unsubAgent();
      unsubRoster?.();
      unsubChallenge?.();
      unsubEnvironment?.();
      clearInterval(spokenPoll);
      if (graduationPoll) clearInterval(graduationPoll);
      document.removeEventListener("innate:camera-reset", onCameraReset);
      document.removeEventListener("innate:play-intro", onPlayIntro);
      document.removeEventListener("pointerdown", onOutsideClick, true);
      root.removeEventListener("change", onSceneChange, true);
      sceneCue?.();
      opts.onCreateAgent?.(() => {});
      uncue();
      hideDragHint();
      panel.setOffers([]);
      panel.setComposerAsk(null);
      panel.setDisplayName(null);
      document.body.classList.remove("story-active");
      if (opts.directivesEl) opts.directivesEl.hidden = false;
      opts.dockDirectives?.(null);
      opts.dockStartStop?.(null);
      dock.remove();
      leaveBtn.remove();
      whiteout.remove();
    },
  };
}
