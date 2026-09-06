// @ts-check
// First-run introduction for the sim challenge panel. Challenges assume
// context a fresh install doesn't have — notably that the tutorial builds the
// skill the first challenge waits for — so the first time the panel appears we
// show a one-time dialog pointing at the simulator tutorial, with a preview of
// the docs page. After that, the panel keeps only a subtle "Tutorial" link in
// its header that reopens this dialog on demand.

import { readFirstRun } from "../onboarding.js";

const TUTORIAL_URL = "https://docs.innate.bot/simulator/building-with-the-simulator";
const PREVIEW_SRC = "/public/challenge-tutorial-preview.jpg";
const SEEN_KEY = "innate.challengeIntroSeen";

/**
 * Show the intro on the panel's first-ever reveal (per browser). Returns a
 * handle to close it (page teardown), or null when it was already seen.
 * @returns {{ close: () => void } | null}
 */
export function maybeShowChallengeIntro() {
  // A completed or skipped first mission already handled the introduction.
  // Keep the explicit Tutorial link, without another automatic onboarding.
  if (storageGet(SEEN_KEY) || ["done", "skipped"].includes(readFirstRun()?.phase)) return null;
  return showChallengeIntro();
}

/**
 * Open the intro dialog unconditionally (the header "Tutorial" link).
 * Dismissing it — by any path — marks it seen.
 * @returns {{ close: () => void }}
 */
export function showChallengeIntro() {
  const prevFocus = document.activeElement;

  const backdrop = document.createElement("div");
  backdrop.className = "challenge-intro-backdrop";
  backdrop.addEventListener("click", dismiss);

  const card = document.createElement("div");
  card.className = "challenge-intro";
  card.setAttribute("role", "dialog");
  card.setAttribute("aria-modal", "true");
  card.setAttribute("aria-label", "About challenges");
  card.tabIndex = -1;
  card.addEventListener("click", (e) => e.stopPropagation());

  const close = document.createElement("button");
  close.type = "button";
  close.className = "challenge-intro-close";
  close.textContent = "✕";
  close.title = "Dismiss";
  close.setAttribute("aria-label", "Dismiss");
  close.addEventListener("click", dismiss);

  const kicker = document.createElement("span");
  kicker.className = "microlabel challenge-intro-kicker";
  kicker.textContent = "Simulator";

  const title = document.createElement("h2");
  title.className = "challenge-intro-title";
  title.textContent = "Meet the challenges";

  const blurb = document.createElement("p");
  blurb.className = "challenge-intro-blurb";
  blurb.textContent =
    "Timed missions for your robot, judged live against the simulator's ground " +
    "truth. Start one, clear its goals before the clock runs out, and the sim " +
    "scores the run.";

  const note = document.createElement("p");
  note.className = "challenge-intro-blurb";
  note.textContent =
    "Most challenges expect skills you've taught the robot first — Victory lap, " +
    "for one, waits for the skill you build in the tutorial.";

  // Docs preview — the whole figure is one link to the tutorial.
  const figure = document.createElement("a");
  figure.className = "challenge-intro-figure";
  figure.href = TUTORIAL_URL;
  figure.title = TUTORIAL_URL;
  figure.target = "_blank";
  figure.rel = "noopener noreferrer";
  const shot = document.createElement("img");
  shot.src = PREVIEW_SRC;
  shot.width = 880;
  shot.height = 489;
  shot.alt = "Docs: Building with the Simulator";
  const chip = document.createElement("span");
  chip.className = "challenge-intro-chip";
  chip.textContent = "docs.innate.bot ↗";
  figure.append(shot, chip);

  const caption = document.createElement("p");
  caption.className = "challenge-intro-caption";
  caption.textContent = "Building with the Simulator — your first skill, end to end, in about two minutes.";

  const cta = document.createElement("a");
  cta.className = "challenge-intro-cta";
  cta.href = TUTORIAL_URL;
  cta.target = "_blank";
  cta.rel = "noopener noreferrer";
  cta.textContent = "Open the tutorial";
  cta.addEventListener("click", dismiss);

  const skip = document.createElement("button");
  skip.type = "button";
  skip.className = "challenge-intro-skip";
  skip.textContent = "Explore on my own";
  skip.addEventListener("click", dismiss);

  card.append(close, kicker, title, blurb, note, figure, caption, cta, skip);
  backdrop.appendChild(card);
  document.body.appendChild(backdrop);
  document.addEventListener("keydown", onKey);
  card.focus();

  /** @param {KeyboardEvent} e */
  function onKey(e) {
    if (e.key === "Escape") dismiss();
  }

  function dismiss() {
    storageSet(SEEN_KEY, "1");
    closeNow();
    if (prevFocus instanceof HTMLElement) prevFocus.focus();
  }

  function closeNow() {
    document.removeEventListener("keydown", onKey);
    backdrop.remove();
  }

  // close() tears down without marking seen — page navigation, not a choice.
  return { close: closeNow };
}

/** localStorage can throw (private mode / disabled) — never let it break the page. */
/** @param {string} key @returns {string | null} */
function storageGet(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

/** @param {string} key @param {string} value */
function storageSet(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* ignore — a non-persisted dismissal still hides it for this page view */
  }
}
